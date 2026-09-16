"""Lifecycle hints remain constant-size and independent of database reads."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from shared import agent_snapshot, live_announce
from shared.config import settings


@pytest.fixture(autouse=True)
def _forbid_snapshot_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("lifecycle announcements must not read an agent snapshot")

    monkeypatch.setattr(agent_snapshot, "select_one", forbidden)


@pytest.mark.parametrize(
    ("publisher", "role"),
    [
        (live_announce.publish_agent_spawned_sync, "agent_spawned"),
        (live_announce.publish_agent_updated_sync, "agent_updated"),
    ],
)
def test_sync_lifecycle_hint_has_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    publisher: Callable[[int], None],
    role: str,
) -> None:
    published: list[tuple[str, str, str]] = []

    def capture(channel: str, payload: str, *, context: str) -> int:
        published.append((channel, payload, context))
        return 0

    monkeypatch.setattr(live_announce, "publish_best_effort_sync", capture)
    publisher(7)
    assert len(published) == 1
    channel, payload, context = published[0]
    assert channel == settings.data_plane.events_channel
    assert context == role
    assert json.loads(payload) == {"role": role, "agent_id": 7}


async def test_async_lifecycle_hint_needs_no_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str, str]] = []

    async def capture(channel: str, payload: str, *, context: str) -> int:
        published.append((channel, payload, context))
        return 0

    monkeypatch.setattr(live_announce, "publish_best_effort", capture)
    await live_announce.publish_agent_updated(7)
    assert len(published) == 1
    channel, payload, context = published[0]
    assert channel == settings.data_plane.events_channel
    assert context == "agent_updated"
    assert json.loads(payload) == {"role": "agent_updated", "agent_id": 7}
