"""Redis protocol diagnostic observes runtime credentials without ACL repair."""

from unittest.mock import MagicMock

import pytest
from redis.exceptions import AuthenticationError

from services.healthchecks import redis_acl


def test_ping_uses_runtime_url_with_bounded_socket_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    from_url = MagicMock(return_value=client)
    monkeypatch.setattr(redis_acl.redis.Redis, "from_url", from_url)
    redis_acl._ping("redis://runtime:credential@127.0.0.1:6380/0")
    from_url.assert_called_once_with(
        "redis://runtime:credential@127.0.0.1:6380/0", socket_connect_timeout=3, socket_timeout=3
    )
    client.__enter__.return_value.ping.assert_called_once()


def test_rejected_credential_is_reported_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.__enter__.return_value.ping.side_effect = AuthenticationError("denied")

    def _fake_from_url(*_a: object, **_kw: object) -> MagicMock:
        return client

    monkeypatch.setattr(redis_acl.redis.Redis, "from_url", _fake_from_url)
    with pytest.raises(AuthenticationError):
        redis_acl._ping("redis://127.0.0.1:6380/0")
    assert not hasattr(redis_acl, "check")
    assert not hasattr(redis_acl, "main")
