"""`emitted_events`: the real emitter delivers into a list, and nothing is patched."""

from __future__ import annotations

import pytest

from base import telemetry
from base.telemetry.delivery.state import process_state
from base.telemetry.tests.capture.event_capture import EmittedEvents, capturing_events
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


def test_events_come_back_in_emit_order_with_their_fields(emitted_events: EmittedEvents) -> None:
    telemetry.emit("telemetry", "understanding_chunk_failed", level="warning", attributes={"n": 1})
    telemetry.emit("telemetry", "understanding_group_failed", attributes={"n": 2})
    telemetry.emit("telemetry", "understanding_chunk_failed", attributes={"n": 3})

    assert emitted_events.names() == [
        "understanding_chunk_failed",
        "understanding_group_failed",
        "understanding_chunk_failed",
    ]
    assert len(emitted_events) == 3
    assert emitted_events.attributes("understanding_chunk_failed") == [{"n": 1}, {"n": 3}]
    (first, *_) = emitted_events.named("understanding_chunk_failed")
    assert (first.category, first.level, first.source) == ("telemetry", "warning", "system")
    assert [event.event_name for event in emitted_events] == emitted_events.names()


def test_the_registry_check_of_the_real_emitter_still_runs(
    emitted_events: EmittedEvents,
) -> None:
    with pytest.raises(ValueError, match="unregistered event_name"):
        telemetry.emit("telemetry", "no_such_event_declared_anywhere")
    with pytest.raises(ValueError, match="contradicts the registry"):
        telemetry.emit("audit", "understanding_chunk_failed")
    assert len(emitted_events) == 0


def test_clear_forgets_what_was_emitted_so_far(emitted_events: EmittedEvents) -> None:
    telemetry.emit("telemetry", "understanding_chunk_failed")
    emitted_events.clear()
    telemetry.emit("telemetry", "understanding_group_failed")
    assert emitted_events.names() == ["understanding_group_failed"]


def test_an_event_emitted_through_the_exposed_producer_lands_in_the_capture(
    emitted_events: EmittedEvents,
) -> None:
    """Code that is handed `producer=` instead of reading the process pipeline writes to the same
    list: the one capture serves both routes."""
    telemetry.emit(
        "telemetry",
        "understanding_chunk_failed",
        attributes={"n": 1},
        producer=emitted_events.producer,
    )
    assert emitted_events.attributes("understanding_chunk_failed") == [{"n": 1}]


def test_a_followed_view_reads_what_has_been_emitted_at_each_use(
    emitted_events: EmittedEvents,
) -> None:
    view = emitted_events.following("understanding_chunk_failed")
    assert view == []
    assert len(view) == 0
    telemetry.emit("telemetry", "understanding_chunk_failed", attributes={"n": 1})
    telemetry.emit("telemetry", "understanding_group_failed", attributes={"n": 2})
    telemetry.emit("telemetry", "understanding_chunk_failed", attributes={"n": 3})
    assert view == [{"n": 1}, {"n": 3}]
    assert [row["n"] for row in view] == [1, 3]
    assert view[-1] == {"n": 3}


def test_the_previous_pipeline_binding_is_put_back() -> None:
    before = process_state["pipeline"]
    with capturing_events() as capture:
        assert process_state["pipeline"] is not before
        telemetry.emit("telemetry", "understanding_chunk_failed")
        assert capture.names() == ["understanding_chunk_failed"]
    assert process_state["pipeline"] is before
