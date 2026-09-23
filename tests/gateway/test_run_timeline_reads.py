"""Complete bounded reads and independent timeline I/O branches."""

from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import pytest
from fastapi import HTTPException

from gateway.routers import _run_timeline_events as reads
from gateway.routers import run_timeline as timeline
from shared.turn_identity import bind_turn_identity, current_turn_agent_id


@pytest.mark.parametrize("tied", [False, True])
def test_event_reads_keep_every_row_without_growing_offsets(
    monkeypatch: pytest.MonkeyPatch, tied: bool
) -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    stop = start + timedelta(microseconds=8)
    # The payload timestamp is deliberately unrelated to its Loki timestamp.
    rows: list[tuple[datetime, dict[str, object]]] = [
        (start + timedelta(microseconds=4 if tied else i), {"id": i, "ts": start}) for i in range(9)
    ]
    calls: list[dict[str, object]] = []

    def query(
        *, from_: datetime, to: datetime, offset: int, limit: int, **kwargs: object
    ) -> tuple[list[dict[str, object]], bool]:
        calls.append({**kwargs, "from_": from_, "to": to, "offset": offset, "limit": limit})
        matching = [row for stamp, row in rows if from_ <= stamp <= to]
        return matching[offset : offset + limit], len(matching) > offset + limit

    monkeypatch.setattr(reads, "_PAGE_SIZE", 2)
    monkeypatch.setattr(reads.loki_events, "query_events", query)
    result = reads.query_all_events(405, start, stop, event_names=("turn_end",))
    assert result == [row for _, row in rows]
    assert all(call["limit"] == 2 for call in calls)
    assert all(call["direction"] == "forward" for call in calls)
    if not tied:
        assert all(
            call["offset"] == 0 or (call["from_"], call["to"]) != (start, stop) for call in calls
        )
    else:
        assert any(call["offset"] == 8 for call in calls)
        # Both inclusive halves may contain the entire tied burst. Each may
        # page it once; repeatedly bisecting this unchanged page is a regression.
        assert len(calls) <= 11


def test_small_event_window_is_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    calls: list[dict[str, object]] = []

    def query(**kwargs: object) -> tuple[list[dict[str, object]], bool]:
        calls.append(kwargs)
        return [{"id": 1}], False

    monkeypatch.setattr(reads.loki_events, "query_events", query)
    assert reads.query_all_events(405, start, start, event_names=("compact",)) == [{"id": 1}]
    assert len(calls) == 1


@pytest.mark.parametrize("backend_fails", [False, True])
def test_strip_overlaps_events_and_joins_with_request_context(
    monkeypatch: pytest.MonkeyPatch, backend_fails: bool
) -> None:
    start = datetime(2026, 9, 22, tzinfo=UTC)
    strip_started, events_started, strip_finished = Event(), Event(), Event()

    def strip(*args: object) -> tuple[list[object], bool]:
        assert current_turn_agent_id() == 405
        assert args == (405, start, start + timedelta(hours=1), 7)
        strip_started.set()
        assert events_started.wait(2), "event read did not overlap strip"
        strip_finished.set()
        return [], True

    def events(*args: object) -> list[dict[str, object]]:
        events_started.set()
        assert strip_started.wait(2), "strip did not overlap event read"
        if backend_fails:
            raise httpx.ConnectError("Loki unavailable")
        return []

    monkeypatch.setattr(timeline, "strip_for_window_or_none", strip)
    monkeypatch.setattr(timeline, "_query_all_events", events)

    def narrative(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    def inbounds(*_args: object) -> list[object]:
        return []

    monkeypatch.setattr(timeline, "_narrative_for_window", narrative)
    monkeypatch.setattr(timeline, "_inbounds_for_window", inbounds)
    with bind_turn_identity(405):
        if backend_fails:
            with pytest.raises(HTTPException) as exc:
                timeline.get_run_timeline(405, start, start + timedelta(hours=1), messages_max=7)
            assert exc.value.status_code == 503
        else:
            result = timeline.get_run_timeline(
                405, start, start + timedelta(hours=1), messages_max=7
            )
            assert result.messages == []
            assert result.messages_truncated is True
            assert result.layers is None
            assert result.rows == []
    assert strip_finished.is_set()
