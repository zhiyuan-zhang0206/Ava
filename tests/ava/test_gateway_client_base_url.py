"""On an agent-runner (the conftest default role), the SDK client's base_url
must resolve to gateway_url, not the (empty/removed) gateway_url."""

from typing import Any, cast

import pytest


def test_gateway_client_base_url_is_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings
    from shared.machine import reset_identity, set_identity

    set_identity(role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://cp.example.com")

    import ava._gateway_transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    try:
        client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
        assert str(client.base_url).rstrip("/") == "https://cp.example.com"
    finally:
        reset_identity()


def test_gateway_client_sends_cluster_secret_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the gateway requires auth on every route. The SDK client must
    present the cluster secret as `Authorization: Bearer <secret>`, or every
    `ava.*` gateway call (list_agents / spawn / ...) 401s from inside an agent."""
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t-token")

    import ava._gateway_transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer s3cr3t-token"


def test_gateway_client_no_bearer_when_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty cluster secret (tests / unprovisioned checkout) sends no auth
    header — the gateway fails open in the same case, so the SDK must not send a
    bogus `Bearer ` either."""
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    import ava._gateway_transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
    assert "Authorization" not in client.headers
