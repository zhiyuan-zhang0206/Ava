"""`EventBus`: the handle binds one config to the transport; the shim publish and the handle's
share one body."""

from __future__ import annotations

from typing import Any

import pytest

from base.config import settings
from base.events.live import bus as bus_module
from base.events.live.bus import EventBus, EventBusConfig


class _Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.published: list[tuple[str, str]] = []
        self.closed = False

    async def publish(self, channel: str, payload: str, *, auth_retry: bool = True) -> int:
        self.published.append((channel, payload))
        return 2

    def close(self) -> None:
        self.closed = True


def _bus(url: str = "redis://secret@host:1/0", channel: str = "ava:test") -> EventBus:
    return EventBus(EventBusConfig(redis_url=url, events_channel=channel))


def test_config_hides_the_url_from_repr() -> None:
    assert "secret" not in repr(EventBusConfig(redis_url="redis://secret@h/0", events_channel="c"))


def test_from_settings_reads_the_live_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "events_channel", "ava:other")
    assert EventBus.from_settings().channel == "ava:other"


async def test_the_async_client_is_shared_per_loop_and_dialed_with_the_handles_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def open_client(url: str, *, decode_responses: bool = True) -> Any:
        opened.append(url)
        return _Client(url)

    monkeypatch.setattr(bus_module, "open_async_redis", open_client)
    handle = _bus("redis://one")
    assert handle.async_redis() is handle.async_redis()
    assert opened == ["redis://one"]
    assert _bus("redis://two").async_redis() is not handle.async_redis()


async def test_publish_goes_to_the_events_channel_unless_another_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client("x")

    def open_client(_url: str, *, decode_responses: bool = True) -> Any:
        return client

    monkeypatch.setattr(bus_module, "open_async_redis", open_client)
    handle = _bus(channel="ava:events")

    assert await handle.publish_best_effort("p1", context="t") == 2
    assert await handle.publish_best_effort("p2", channel="ava:page") == 2

    assert client.published == [("ava:events", "p1"), ("ava:page", "p2")]


async def test_a_failed_publish_returns_none_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(bus_module, "open_async_redis", refuse)
    assert await _bus().publish_best_effort("p") is None


def test_sync_publish_uses_a_one_off_client_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client("x")
    sent: list[tuple[str, str]] = []
    client.publish = lambda channel, payload, **_options: sent.append((channel, payload)) or 1  # type: ignore[assignment,method-assign]
    urls: list[str] = []

    def open_client(url: str, *, decode_responses: bool = False) -> Any:
        urls.append(url)
        return client

    monkeypatch.setattr(bus_module, "open_sync_redis", open_client)

    assert _bus("redis://sync", "ava:e").publish_best_effort_sync("p", context="t") == 1
    assert sent == [("ava:e", "p")]
    assert urls == ["redis://sync"]
    assert client.closed
