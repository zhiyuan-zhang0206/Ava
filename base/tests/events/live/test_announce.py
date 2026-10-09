"""Lifecycle hints remain constant-size and independent of database reads."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest

from base.agents.observation import snapshot
from base.events.live import announce
from base.events.live.bus import EventBus


@pytest.fixture(autouse=True)
def _forbid_snapshot_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("lifecycle announcements must not read an agent snapshot")

    monkeypatch.setattr(snapshot, "select_one", forbidden)


class _RecordingBus:
    """An `EventBus` stand-in recording what is published and on which channel."""

    channel = "ava:test-events"

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish_best_effort_sync(self, payload: str, *, context: str) -> int:
        self.published.append((payload, context))
        return 0

    async def publish_best_effort(self, payload: str, *, context: str) -> int:
        self.published.append((payload, context))
        return 0


@pytest.mark.parametrize(
    ("publisher", "role"),
    [
        (announce.publish_agent_spawned_sync, "agent_spawned"),
        (announce.publish_agent_updated_sync, "agent_updated"),
    ],
)
def test_sync_lifecycle_hint_has_no_snapshot(
    publisher: Callable[[EventBus, int], None], role: str
) -> None:
    bus = _RecordingBus()
    publisher(cast(EventBus, bus), 7)
    assert len(bus.published) == 1
    payload, context = bus.published[0]
    assert context == role
    assert json.loads(payload) == {"role": role, "agent_id": 7}


async def test_async_lifecycle_hint_needs_no_connection() -> None:
    bus = _RecordingBus()
    await announce.publish_agent_updated(cast(EventBus, bus), 7)
    assert len(bus.published) == 1
    payload, context = bus.published[0]
    assert context == "agent_updated"
    assert json.loads(payload) == {"role": "agent_updated", "agent_id": 7}
