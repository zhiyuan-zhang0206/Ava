"""Capture the telemetry events a test emits, through the real `emit`.

A test that wants to assert on telemetry requests `emitted_events` instead of replacing
`telemetry.emit`. The real emitter runs, including its registry check (an unregistered
`event_name`, or a category that contradicts the declaration, still raises), and the events
land in a real `EventPipeline` whose `writer` is a list.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event
from base.telemetry.delivery.state import process_state

__all__ = ["EmittedEvents", "capturing_events", "emitted_events"]

_SETTLE_TIMEOUT_S = 5.0


class EmittedEvents:
    """The events emitted in this process since the fixture was requested, in emit order.

    Every read first waits for the pipeline to acknowledge what was queued, so an assertion
    never races the drain thread and no test sleeps."""

    def __init__(self, captured: list[Event], pipeline: EventPipeline) -> None:
        self._captured = captured
        self._pipeline = pipeline

    def _settled(self) -> list[Event]:
        result = self._pipeline.sync(_SETTLE_TIMEOUT_S)
        assert result.status is DrainStatus.COMPLETED, f"event pipeline did not settle: {result}"
        return list(self._captured)

    @property
    def producer(self) -> Callable[[], EventPipeline]:
        """The capture pipeline as a `producer`, for code that is handed one (`producer=...`) instead
        of reading the process pipeline: the events it emits land here too."""
        return lambda: self._pipeline

    def __iter__(self) -> Iterator[Event]:
        return iter(self._settled())

    def __len__(self) -> int:
        return len(self._settled())

    def names(self) -> list[str]:
        """The `event_name` of every event, in emit order."""
        return [event.event_name for event in self._settled()]

    def named(self, event_name: str) -> list[Event]:
        """The events with this name, in emit order."""
        return [event for event in self._settled() if event.event_name == event_name]

    def attributes(self, event_name: str) -> list[dict[str, Any]]:
        """The `attributes` of every event with this name, in emit order."""
        return [event.attributes for event in self.named(event_name)]

    def clear(self) -> None:
        """Forget what was emitted so far (a later read sees only later events)."""
        self._settled()
        self._captured.clear()


@contextmanager
def capturing_events() -> Generator[EmittedEvents]:
    """Bind a list-writing pipeline as this process's event pipeline for the block.

    The previously bound pipeline (usually none) is put back afterwards. The only events
    captured are those emitted by this process while the block runs; an exec child or any
    other process keeps its own pipeline. A test that builds its own `EventPipeline` and binds
    it does not use this."""
    captured: list[Event] = []
    pipeline = EventPipeline(writer=captured.extend, flush_interval_s=0.01)
    previous = process_state["pipeline"]
    process_state["pipeline"] = pipeline
    try:
        yield EmittedEvents(captured, pipeline)
    finally:
        process_state["pipeline"] = previous
        pipeline.stop()


@pytest.fixture
def emitted_events() -> Iterator[EmittedEvents]:
    """The events this test emits, captured for the whole test (see `capturing_events`)."""
    with capturing_events() as events:
        yield events
