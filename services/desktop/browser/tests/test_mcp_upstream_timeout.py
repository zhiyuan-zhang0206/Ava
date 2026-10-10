"""Connect readers are consumed by the existing stop/initialize race."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from mcp import ClientSession

from services.desktop.browser import mcp_upstream
from services.desktop.browser.mcp_upstream import (
    _initialize_until_stop_or_timeout,
    _StoppingError,
)


class Initializer:
    def __init__(self, *, blocked: bool = False, error: Exception | None = None) -> None:
        self.blocked = blocked
        self.error = error
        self.cancelled = False
        self.calls = 0

    async def initialize(self) -> None:
        self.calls += 1
        try:
            if self.blocked:
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_initialization_reads_current_timeout_for_each_race() -> None:
    timeout = 0.01
    values: list[float] = []

    def reader() -> float:
        values.append(timeout)
        return timeout

    session = Initializer(blocked=True)
    assert values == []
    with pytest.raises(TimeoutError, match="initialize"):
        await _initialize_until_stop_or_timeout(
            cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=reader
        )
    assert session.cancelled
    timeout = 1.0
    session.blocked = False
    await _initialize_until_stop_or_timeout(
        cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=reader
    )
    assert session.calls == 2
    assert values == [0.01, 1.0]


async def test_stop_still_cancels_pending_initialization() -> None:
    stop = asyncio.Event()
    stop.set()
    session = Initializer(blocked=True)
    with pytest.raises(_StoppingError):
        await _initialize_until_stop_or_timeout(
            cast(ClientSession, session), stop, connect_timeout_reader=lambda: 10.0
        )
    assert session.cancelled


async def test_original_initialization_error_is_preserved() -> None:
    error = RuntimeError("initialize failed")
    session = Initializer(error=error)
    with pytest.raises(RuntimeError) as observed:
        await _initialize_until_stop_or_timeout(
            cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=lambda: 1.0
        )
    assert observed.value is error


async def test_create_upstream_passes_reader_and_closes_failed_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    session = Initializer(blocked=True)
    reads: list[float] = []

    @asynccontextmanager
    async def stdio(_params: object) -> AsyncGenerator[tuple[object, object], None]:
        try:
            yield object(), object()
        finally:
            closed.append("stdio")

    @asynccontextmanager
    async def client(*_args: object, **_kwargs: object) -> AsyncGenerator[Initializer, None]:
        try:
            yield session
        finally:
            closed.append("session")

    def timeout_reader() -> float:
        reads.append(0.01)
        return 0.01

    monkeypatch.setattr(mcp_upstream, "stdio_client", stdio)
    monkeypatch.setattr(mcp_upstream, "ClientSession", client)
    with pytest.raises(TimeoutError):
        await mcp_upstream._create_upstream(
            "http://example.test", asyncio.Event(), connect_timeout_reader=timeout_reader
        )
    assert reads == [0.01]
    assert session.cancelled
    assert closed == ["session", "stdio"]
