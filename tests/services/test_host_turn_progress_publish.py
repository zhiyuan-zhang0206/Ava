"""Hosted turn-progress snapshots and their agent-host heartbeat publication."""

from __future__ import annotations

import json
from typing import cast

import pytest

from agent import _turn_progress as progress
from services.agent_host import daemon as host_daemon


def test_turn_progress_snapshot_keeps_only_the_latest_three_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = 5815
    now = [10.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: now[0])

    try:
        for timestamp in (10.0, 20.0, 30.0, 40.0):
            now[0] = timestamp
            progress.mark_turn_progress(agent_id)
        now[0] = 45.0

        assert progress.turn_progress_snapshot(agent_id) == {
            "age_s": 5.0,
            "last_marks": [20.0, 30.0, 40.0],
        }
    finally:
        progress._PROGRESS.pop(agent_id, None)


def test_turn_progress_snapshot_is_none_without_a_mark() -> None:
    agent_id = 5816
    progress._PROGRESS.pop(agent_id, None)
    assert progress.turn_progress_snapshot(agent_id) is None


async def test_agent_host_publishes_active_snapshots_and_refreshes_empty_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = 5817
    progress._PROGRESS.pop(agent_id, None)
    progress.mark_turn_progress(agent_id)
    writes: list[tuple[str, str, int]] = []

    class FakeRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            writes.append((key, value, ex))

    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", FakeRedis)
    try:
        await host_daemon._publish_turn_progress_heartbeat("runner-a", {agent_id})
        await host_daemon._publish_turn_progress_heartbeat("runner-a", set())
    finally:
        progress._PROGRESS.pop(agent_id, None)

    first_key, first_payload, first_ttl = writes[0]
    assert first_key == "host_turn_progress:runner-a"
    assert first_ttl == 60
    assert set(json.loads(first_payload)) == {str(agent_id)}
    assert len(json.loads(first_payload)[str(agent_id)]["last_marks"]) == 1
    assert writes[1] == ("host_turn_progress:runner-a", "{}", 60)


async def test_agent_host_progress_publish_failure_is_debug_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", BrokenRedis)

    await host_daemon._publish_turn_progress_heartbeat("runner-a", set())


async def test_existing_ownership_beat_publishes_after_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class StopBeatError(Exception):
        pass

    class FakeHost:
        async def renew_ownership(self) -> None:
            calls.append("renew")

    class FakeScheduler:
        active_agents = frozenset({41, 42})

    class FakeLiveness:
        def beat(self) -> None:
            calls.append("liveness")

    async def fake_publish(machine: str, active_agents: object) -> None:
        calls.append(("publish", machine, active_agents))

    async def stop_sleep(delay: float) -> None:
        calls.append(("sleep", delay))
        raise StopBeatError

    monkeypatch.setattr(host_daemon, "_publish_turn_progress_heartbeat", fake_publish)
    monkeypatch.setattr(host_daemon.asyncio, "sleep", stop_sleep)

    with pytest.raises(StopBeatError):
        await host_daemon._beat_forever(
            cast(host_daemon.Liveness, FakeLiveness()),
            cast(host_daemon.AgentHost, FakeHost()),
            cast(host_daemon.TurnScheduler, FakeScheduler()),
            "runner-a",
        )

    assert calls == [
        "renew",
        "liveness",
        ("publish", "runner-a", frozenset({41, 42})),
        ("sleep", 15.0),
    ]
