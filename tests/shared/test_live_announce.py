"""Wire contracts for lifecycle SSE announcements."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest

from shared import live_announce
from shared.agent_snapshot import AgentSnapshot
from shared.agents import AgentStatus

_AT = datetime(2026, 9, 1, 4, 5, 6, tzinfo=UTC)
_FULL_SNAPSHOT_FIELDS = {
    "agent_id",
    "spawner",
    "fork_source_agent_id",
    "fork_source_checkpoint_id",
    "status",
    "pid",
    "spawned_at",
    "started_at",
    "last_active_at",
    "last_inbound_at",
    "label",
    "machine",
    "supports_vision",
    "liveness_state",
    "last_probe_at",
    "notices_awaiting_response",
    "unread_notice_count",
    "heartbeat_paused_until",
}


def _full_snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=7,
        spawner="agent:3",
        fork_source_agent_id=3,
        fork_source_checkpoint_id="checkpoint-7",
        status=AgentStatus.IDLING,
        pid=1234,
        spawned_at=_AT,
        started_at=_AT,
        last_active_at=_AT,
        last_inbound_at=_AT,
        label="alpha",
        machine="runner-a",
        supports_vision=True,
        liveness_state="online",
        last_probe_at=_AT,
        notices_awaiting_response=[],
        unread_notice_count=2,
        heartbeat_paused_until=None,
    )


@pytest.mark.parametrize(
    ("publisher", "expected_role", "expected_context"),
    [
        (live_announce.publish_agent_spawned_sync, "agent_spawned", "agent_spawned"),
        (live_announce.publish_agent_updated_sync, "agent_updated", "agent_updated"),
    ],
)
def test_lifecycle_sse_events_serialize_the_full_agent_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    publisher: Callable[[psycopg.Connection[Any], int], None],
    expected_role: str,
    expected_context: str,
) -> None:
    """SSE lifecycle updates retain fields omitted by the REST roster summary."""
    snapshot = _full_snapshot()
    published: list[tuple[str, dict[str, Any]]] = []

    def _select_one(_conn: psycopg.Connection[Any], _agent_id: int) -> AgentSnapshot:
        return snapshot

    def _capture(_channel: str, payload: str, *, context: str) -> int:
        published.append((context, cast(dict[str, Any], json.loads(payload))))
        return 0

    monkeypatch.setattr(live_announce, "select_one", _select_one)
    monkeypatch.setattr(live_announce, "publish_best_effort_sync", _capture)

    publisher(cast(psycopg.Connection[Any], object()), snapshot.agent_id)

    assert len(published) == 1
    context, event = published[0]
    assert context == expected_context
    assert event["role"] == expected_role
    assert event["agent_id"] == snapshot.agent_id
    assert set(event["snapshot"]) == _FULL_SNAPSHOT_FIELDS
    assert event["snapshot"]["fork_source_checkpoint_id"] == "checkpoint-7"
    assert event["snapshot"]["last_probe_at"] == "2026-09-01T04:05:06Z"
