"""Hosted turn-progress snapshots and their agent-host heartbeat publication."""

from __future__ import annotations

import asyncio
import json
import logging
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", BrokenRedis)

    with caplog.at_level(logging.DEBUG, logger=host_daemon._log.name):
        await host_daemon._publish_turn_progress_heartbeat("runner-a", set())

    records = [record for record in caplog.records if record.name == host_daemon._log.name]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].exc_info is not None


async def test_hung_progress_set_does_not_stop_repeated_ownership_renewal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    third_set = asyncio.Event()

    class HungRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            calls.append("set")
            if calls.count("set") == 3:
                third_set.set()
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("set_cancelled")

    class FakeHost:
        async def renew_ownership(self) -> None:
            calls.append("renew")

    class FakeLiveness:
        def beat(self) -> None:
            calls.append("beat")

    class FakeScheduler:
        active_agents: frozenset[int] = frozenset()

    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", HungRedis)
    monkeypatch.setattr(host_daemon, "_TURN_PROGRESS_PUBLISH_TIMEOUT_S", 0.01, raising=False)
    monkeypatch.setattr(host_daemon, "_LIVENESS_BEAT_STEP_S", 0.01)
    with caplog.at_level(logging.WARNING, logger=host_daemon._log.name):
        task = asyncio.create_task(
            host_daemon._beat_forever(
                cast(host_daemon.Liveness, FakeLiveness()),
                cast(host_daemon.AgentHost, FakeHost()),
                cast(host_daemon.TurnScheduler, FakeScheduler()),
                "runner-a",
            )
        )
        try:
            await asyncio.wait_for(third_set.wait(), timeout=1.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert calls == ["renew", "beat", "set", "set_cancelled"] * 3
    warnings = [record for record in caplog.records if record.name == host_daemon._log.name]
    assert len(warnings) == 2
    assert all("turn-progress heartbeat publish exceeded" in record.message for record in warnings)


async def test_progress_publish_propagates_cancellation_without_failure_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class HungRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", HungRedis)
    with caplog.at_level(logging.DEBUG, logger=host_daemon._log.name):
        task = asyncio.create_task(host_daemon._publish_turn_progress_heartbeat("runner-a", set()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert cancelled.is_set()
    assert not [record for record in caplog.records if record.name == host_daemon._log.name]


async def _read_resp_command(reader: asyncio.StreamReader) -> list[bytes]:
    line = await reader.readline()
    if not line:
        return []
    assert line.startswith(b"*")
    parts: list[bytes] = []
    for _ in range(int(line[1:])):
        size = await reader.readline()
        assert size.startswith(b"$")
        parts.append((await reader.readexactly(int(size[1:]) + 2))[:-2])
    return parts


async def test_timed_out_redis_connection_is_released_before_next_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_disconnected = asyncio.Event()
    writes: list[list[bytes]] = []
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            while parts := await _read_resp_command(reader):
                if parts[0] == b"HELLO":
                    writer.write(b"%1\r\n+proto\r\n:3\r\n")
                elif parts[0] == b"SET":
                    writes.append(parts)
                    if len(writes) == 1:
                        assert await reader.read() == b""
                        first_disconnected.set()
                        return
                    writer.write(b"+OK\r\n")
                else:
                    assert parts[0] in {b"CLIENT", b"PING"}
                    writer.write(b"+PONG\r\n" if parts[0] == b"PING" else b"+OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.remove(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client = host_daemon.shared.redis_client.open_async_redis(
        f"redis://127.0.0.1:{server.sockets[0].getsockname()[1]}/0"
    )
    client.connection_pool.max_connections = 1
    monkeypatch.setattr(host_daemon.shared.redis_client, "get_async_redis", lambda: client)
    monkeypatch.setattr(host_daemon, "_TURN_PROGRESS_PUBLISH_TIMEOUT_S", 0.1, raising=False)
    try:
        with caplog.at_level(logging.DEBUG, logger=host_daemon._log.name):
            async with asyncio.timeout(2.0):
                await host_daemon._publish_turn_progress_heartbeat("runner-a", set())
                await first_disconnected.wait()
                assert not client.connection_pool._in_use_connections
                await host_daemon._publish_turn_progress_heartbeat("runner-a", set())
                assert writes == [[b"SET", b"host_turn_progress:runner-a", b"{}", b"EX", b"60"]] * 2
                assert not client.connection_pool._in_use_connections
        records = [record for record in caplog.records if record.name == host_daemon._log.name]
        assert len(records) == 1  # The second response was accepted, not swallowed as a failure.
        assert "turn-progress heartbeat publish exceeded" in records[0].message
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


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
