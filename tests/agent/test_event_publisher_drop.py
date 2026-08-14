"""`AgentEventPublisher` sheds → one structured `sse_drop` agent_event.

The ops monitor panel's SSE-backlog metric reads `sse_drop` rows from
agent_events; this locks the emit contract (event name, kind values, payload
fields) without a live Redis — emit() is synchronous and the drop report is
rate-limited but fires on the first drop (monotonic clock is far past 0).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shared.event_publisher import AgentEventPublisher


def _publisher(maxsize: int = 2) -> AgentEventPublisher:
    # redis client unused on the emit path; start() is never called here.
    return AgentEventPublisher(
        MagicMock(), "ava:events", agent_id=42, maxsize=maxsize, publish_timeout=0.1
    )


def test_queue_full_emit_reports_sse_drop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filling the queue sheds the newest event and logs one structured
    `sse_drop` line with kind=queue_full and the payload fields the panel
    reads (n, aid, queue_size)."""
    warns: list[dict] = []
    fake_logger = MagicMock()
    fake_logger.warning.side_effect = lambda _msg, **kw: warns.append(kw)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("shared.event_publisher.logger", fake_logger)

    pub = _publisher(maxsize=2)
    pub.emit("one")
    pub.emit("two")
    assert warns == [], "no drop yet — queue has room"
    pub.emit("three")  # queue full -> shed -> first (rate-limited) report
    pub.emit("four")  # still full -> shed, same report window -> no second line

    assert len(warns) == 1  # pyright: ignore[reportUnknownArgumentType]
    kw = warns[0]
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "queue_full"
    assert kw["n"] == 1  # delta since last report — the second shed waits
    assert kw["aid"] == 42
    assert kw["queue_size"] == 2

    # Simulate the rate-limit window elapsing: the next shed reports the
    # accumulated delta (drops 2..3), so no drop is lost to the rate limit.
    pub._last_warn = 0.0
    pub.emit("five")  # drop 3 -> reports delta 3-1=2
    pub.emit("six")  # drop 4 -> same report window, no second line
    assert len(warns) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert warns[1]["n"] == 2


def test_publish_error_emit_reports_sse_drop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish failure (redis down / slow) sheds with kind=publish_error and
    carries the exception repr as detail."""
    warns: list[dict] = []
    fake_logger = MagicMock()
    fake_logger.warning.side_effect = lambda _msg, **kw: warns.append(kw)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("shared.event_publisher.logger", fake_logger)

    pub = _publisher()
    pub._note_drop("publish_error", detail="ConnectionError('boom')")
    assert len(warns) == 1  # pyright: ignore[reportUnknownArgumentType]
    kw = warns[0]
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "publish_error"
    assert kw["detail"] == "ConnectionError('boom')"
    assert kw["n"] == 1
