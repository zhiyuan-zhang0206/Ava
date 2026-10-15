import pytest
import redis

from ava.boot import process_clients


def test_connect_redis_applies_resilience_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, dict[str, object]] = {}

    def fake_from_url(url: str, **kwargs: object) -> object:
        captured["kwargs"] = dict(kwargs)
        return object()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(fake_from_url))

    process_clients().redis._get()

    kw = captured["kwargs"]
    assert kw["decode_responses"] is True
    assert kw["socket_keepalive"] is True
    assert kw["socket_connect_timeout"] == 5.0
    assert kw["health_check_interval"] == 30
    assert "socket_keepalive_options" in kw
    assert kw["socket_timeout"] == 10.0
