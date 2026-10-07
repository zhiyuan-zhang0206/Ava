"""The live event-bus Redis helper reads the configured URL and forwards options."""

from __future__ import annotations

from base.events.live.bus import EventBus


def test_sync_redis_ping() -> None:
    client = EventBus.from_settings().sync_redis()
    try:
        assert client.ping() is True  # pyright: ignore[reportUnknownMemberType]
    finally:
        client.close()


def test_sync_redis_decode_responses_passthrough() -> None:
    client = EventBus.from_settings().sync_redis(decode_responses=True)
    try:
        client.set("ava:test:connect-helper", "v")
        assert client.get("ava:test:connect-helper") == "v"  # str, not bytes
    finally:
        client.delete("ava:test:connect-helper")
        client.close()
