"""Stand-ins for the announce hints (`base.events.live.announce`), whose first argument is the
`EventBus` they publish on."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.events.live import bus as bus_module
from base.events.live.bus import EventBus


def recording(into: list[int]) -> Callable[[object, int], None]:
    """An announce stand-in that records the agent id it is called with."""

    def announce(_bus: object, agent_id: int) -> None:
        into.append(agent_id)

    return announce


def patch_announcements(
    monkeypatch: pytest.MonkeyPatch, module: object, *, changed: list[int], updated: list[int]
) -> None:
    """Replace the impersonation-changed and agent-updated hints `module` imported with
    recorders of the agent ids they are called with."""
    monkeypatch.setattr(module, "publish_impersonation_changed_sync", recording(changed))
    monkeypatch.setattr(module, "publish_agent_updated_sync", recording(updated))


def patch_async_redis(monkeypatch: pytest.MonkeyPatch, client: Callable[[], Any]) -> None:
    """Make every `EventBus.async_redis()` return `client()` (the shared per-loop client)."""

    def async_redis(_bus: EventBus) -> Any:
        return client()

    monkeypatch.setattr(EventBus, "async_redis", async_redis)


def patch_open_async_redis(
    monkeypatch: pytest.MonkeyPatch, open_client: Callable[..., Any]
) -> None:
    """Make every `EventBus.open_async_redis()` open its client through `open_client(url, ...)`."""
    monkeypatch.setattr(bus_module, "open_async_redis", open_client)


def patch_sync_redis(monkeypatch: pytest.MonkeyPatch, client: Callable[[], Any]) -> None:
    """Make every `EventBus.sync_redis()` return `client()`."""

    def sync_redis(_bus: EventBus, *, decode_responses: bool = False) -> Any:
        del decode_responses
        return client()

    monkeypatch.setattr(EventBus, "sync_redis", sync_redis)


class _PublishRecorder:
    """A sync Redis client (a context manager) that records every `publish`."""

    def __init__(self, published: list[tuple[str, str]]) -> None:
        self._published = published

    def __enter__(self) -> _PublishRecorder:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def publish(self, channel: str, frame: str) -> None:
        self._published.append((channel, frame))


def record_publishes(monkeypatch: pytest.MonkeyPatch, published: list[tuple[str, str]]) -> None:
    """Make every `EventBus.sync_redis()` a client that appends `(channel, frame)` to `published`."""
    patch_sync_redis(monkeypatch, lambda: _PublishRecorder(published))
