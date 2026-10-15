from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_open_async_redis
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher


class _IdleScheduler:
    @property
    def active_agents(self) -> frozenset[int]:
        return frozenset()

    @property
    def restart_required(self) -> bool:
        return False

    def wake(self, agent_id: int) -> asyncio.Task[None] | None:
        raise AssertionError("the reconnect path must not wake an agent")

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return None

    def reaped_successor(self, agent_id: int) -> asyncio.Task[None] | None:
        return None


async def test_reconnect_logs_warning_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:

    class _HangingPubSub:
        async def psubscribe(self, _pattern: str) -> None:
            return None

        async def get_message(self, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            return None

    class _Redis:
        def pubsub(self, **_kwargs: object) -> _HangingPubSub:
            return _HangingPubSub()

        async def aclose(self) -> None:
            return None

    def _open(_url: str, **_kw: object) -> _Redis:
        return _Redis()

    patch_open_async_redis(monkeypatch, _open)
    disp = InboundWakeDispatcher(
        EventBus.from_settings(),
        _IdleScheduler(),
        subscription_read_timeout_s=0.01,
        subscription_read_deadline_grace_s=0.01,
        reconnect_delay_s=0.0,
    )
    task = asyncio.create_task(disp.run())

    def _reconnects() -> list[dict[str, Any]]:
        return [
            record
            for record in loguru_records
            if record["extra"].get("event") == "host_dispatcher_reconnect"
        ]

    try:
        deadline = asyncio.get_running_loop().time() + 1.0
        while not _reconnects() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    reconnects = _reconnects()
    assert reconnects, "the half-open read never reconnected"
    assert [record["level"].name for record in reconnects] == ["WARNING"] * len(reconnects)
    assert [record["exception"] for record in reconnects] == [None] * len(reconnects)
    assert all("exception_type" in record["extra"] for record in reconnects)
