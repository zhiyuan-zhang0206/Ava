"""Complete bounded reads and independent timeline I/O branches."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from base.db import Database
from base.db.tests.fakes import fake_database
from base.native_process.turn_identity import bind_turn_identity, current_turn_agent_id
from gateway.events import telemetry_rows
from gateway.run_timeline import _events as reads
from gateway.run_timeline import router as timeline
from gateway.run_timeline.strip import SegmentReadCache


@contextmanager
def _no_connection(**_kwargs: object):  # type: ignore[no-untyped-def]
    yield object()


def test_event_reads_page_through_every_row_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, object]] = [{"id": i} for i in range(9)]
    calls: list[dict[str, object]] = []

    def query(
        _conn: object, *, offset: int, limit: int, **kwargs: object
    ) -> tuple[list[dict[str, object]], bool]:
        calls.append({**kwargs, "offset": offset, "limit": limit})
        return rows[offset : offset + limit], len(rows) > offset + limit

    monkeypatch.setattr(reads, "_PAGE_SIZE", 2)
    monkeypatch.setattr(telemetry_rows, "query_events", query)
    start = datetime(2026, 9, 22, tzinfo=UTC)
    result = reads.query_all_events(
        fake_database(_no_connection),
        405,
        start,
        start + timedelta(hours=1),
        event_names=("turn_end",),
    )
    assert result == rows
    assert [call["offset"] for call in calls] == [0, 2, 4, 6, 8]
    assert all(call["limit"] == 2 and call["direction"] == "forward" for call in calls)
    assert all(call["agent_id"] == 405 and call["event_names"] == ["turn_end"] for call in calls)


def test_small_event_window_is_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    calls: list[dict[str, object]] = []

    def query(_conn: object, **kwargs: object) -> tuple[list[dict[str, object]], bool]:
        calls.append(kwargs)
        return [{"id": 1}], False

    monkeypatch.setattr(telemetry_rows, "query_events", query)
    database = fake_database(_no_connection)
    assert reads.query_all_events(database, 405, start, start, event_names=("turn_end",)) == [
        {"id": 1}
    ]
    assert len(calls) == 1


def _request() -> Request:
    state = SimpleNamespace(db=Database.from_settings(), strip_cache=SegmentReadCache())
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


def test_strip_overlaps_events_and_joins_with_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    strip_started, events_started, strip_finished = Event(), Event(), Event()

    def strip(*args: object) -> tuple[list[object], bool]:
        assert current_turn_agent_id() == 405
        assert isinstance(args[0], SegmentReadCache)
        assert args[2:] == (405, start, start + timedelta(hours=1), 7)
        strip_started.set()
        assert events_started.wait(2), "event read did not overlap strip"
        strip_finished.set()
        return [], True

    def events(*args: object) -> list[dict[str, object]]:
        events_started.set()
        assert strip_started.wait(2), "strip did not overlap event read"
        return []

    monkeypatch.setattr(timeline, "strip_for_window_or_none", strip)
    monkeypatch.setattr(timeline, "_query_all_events", events)

    def narrative(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    def inbounds(_db: object, *_args: object) -> list[object]:
        return []

    monkeypatch.setattr(timeline, "_narrative_for_window", narrative)
    monkeypatch.setattr(timeline, "_inbounds_for_window", inbounds)
    with bind_turn_identity(405):
        result = timeline.get_run_timeline(
            _request(), 405, start, start + timedelta(hours=1), messages_max=7
        )
        assert result.messages == []
        assert result.messages_truncated is True
        assert result.layers is None
        assert result.rows == []
    assert strip_finished.is_set()
