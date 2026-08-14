"""SDK ava.REDIS weak-network posture (audit #689 G3).

The SDK redis handle serves short get/set/publish from the exec sandbox; it
must carry the same resilience kwargs as the shared clients (connect timeout,
keepalive, health check) PLUS a socket read bound — a half-dead socket would
otherwise hang an SDK call on the OS TCP-retransmit timeout (minutes) with no
application-level floor. Task #690 ruling: socket_timeout=10 is safe here
because ava.REDIS has no pubsub long-read (the inbound listener uses its own
aredis client).
"""

import pytest
import redis

from ava import _settings


def test_connect_redis_applies_resilience_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, dict[str, object]] = {}

    def fake_from_url(url: str, **kwargs: object) -> object:
        captured["kwargs"] = dict(kwargs)
        return object()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(fake_from_url))

    _settings._connect_redis()  # pyright: ignore[reportUnknownMemberType]

    kw = captured["kwargs"]
    assert kw["decode_responses"] is True
    # weak-network floor (shared/redis_client.py._RESILIENCE_KWARGS)
    assert kw["socket_keepalive"] is True
    assert kw["socket_connect_timeout"] == 5.0
    assert kw["health_check_interval"] == 30
    assert "socket_keepalive_options" in kw
    # hard read bound — the G3 addition over the shared clients (no pubsub here)
    assert kw["socket_timeout"] == 10.0
