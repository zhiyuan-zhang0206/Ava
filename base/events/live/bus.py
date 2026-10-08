"""`EventBus`: the injected handle to the cluster Redis and its live-events channel.

Components take an `EventBus` (or a client it opened), never the Redis URL or the channel name:
the URL carries the login. A composition root builds the handle once (`EventBus.from_settings()`)
and passes it down. The module-level `get_async_redis()` / `sync_redis()` / `publish_best_effort*`
in `base.events.live.redis_client` remain as the process default that reads the live settings at
each call, a shim that the `ambient-bus` rule (scripts/structure/ambient_state) bans package by
package.

The transport decisions (resilience kwargs, auth-retry, the best-effort publish discipline) stay
in `redis_client`; this class only binds one `EventBusConfig` to them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from base.config import settings
from base.events.live.redis_client import (
    _AuthRetryAsyncRedis,
    _AuthRetrySyncRedis,
    open_async_redis,
    open_sync_redis,
    publish_sync_via,
    publish_via,
)


@dataclass(frozen=True)
class EventBusConfig:
    """What the live-events transport reads to decide where to dial and what to publish on."""

    # The cluster Redis access URL; carries the login.
    redis_url: str = field(repr=False)
    # The one channel every live lifecycle / UI event is published on.
    events_channel: str


def event_bus_config_from_settings() -> EventBusConfig:
    """Build the slice from the live settings (read at each call, like `db_config_from_settings`)."""
    return EventBusConfig(
        redis_url=settings.data_plane.redis_url,
        events_channel=settings.data_plane.events_channel,
    )


class EventBus:
    """One cluster Redis and its events channel, dialed with one `EventBusConfig`."""

    def __init__(self, config: EventBusConfig) -> None:
        self._config = config
        self._warn_last: dict[tuple[str, str], float] = {}
        self._clients: dict[asyncio.AbstractEventLoop, _AuthRetryAsyncRedis] = {}

    @classmethod
    def from_settings(cls) -> EventBus:
        """The composition-root constructor: the config as the live settings hold it now."""
        return cls(event_bus_config_from_settings())

    @property
    def channel(self) -> str:
        """The channel live lifecycle and UI events are published on."""
        return self._config.events_channel

    def open_async_redis(self, *, decode_responses: bool = True) -> _AuthRetryAsyncRedis:
        """A new async client for a caller-owned connection lifecycle (pub/sub subscribers)."""
        return open_async_redis(self._config.redis_url, decode_responses=decode_responses)

    def async_redis(self) -> _AuthRetryAsyncRedis:
        """The shared async client of the running event loop; callers must not close it."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = self.open_async_redis()
            self._clients[loop] = client
        return client

    def sync_redis(self, *, decode_responses: bool = False) -> _AuthRetrySyncRedis:
        """A new synchronous client; the caller owns it and closes it."""
        return open_sync_redis(self._config.redis_url, decode_responses=decode_responses)

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        """Publish `payload` on the shared async client (the events channel unless `channel`
        names another), best-effort: never raises. The receiver count, or None on failure."""
        return await publish_via(
            self.async_redis,
            self.channel if channel is None else channel,
            payload,
            warn_last=self._warn_last,
            context=context,
        )

    def publish_best_effort_sync(
        self,
        payload: str,
        *,
        channel: str | None = None,
        decode_responses: bool = True,
        context: str = "",
    ) -> int | None:
        """Sync counterpart: a one-off client, never raises."""
        return publish_sync_via(
            lambda: self.sync_redis(decode_responses=decode_responses),
            self.channel if channel is None else channel,
            payload,
            warn_last=self._warn_last,
            context=context,
        )
