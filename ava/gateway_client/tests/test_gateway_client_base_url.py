"""On an agent-runner (the conftest default role), the SDK client's base_url
must resolve to gateway_url, not the (empty/removed) gateway_url."""

from typing import Any, cast

import pytest


def test_gateway_client_base_url_is_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.cluster.machine import reset_identity, set_identity
    from base.config import settings

    set_identity(role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://cp.example.com")

    import ava.gateway_client.transport as gc

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
    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t-token")

    import ava.gateway_client.transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer s3cr3t-token"


def test_gateway_client_no_bearer_when_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty cluster secret (tests / unprovisioned checkout) sends no auth
    header — the gateway fails open in the same case, so the SDK must not send a
    bogus `Bearer ` either."""
    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    import ava.gateway_client.transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
    assert "Authorization" not in client.headers


def test_gateway_client_in_an_agent_presents_the_delivered_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent inherits its host's machine API token; the human secret never
    rides along when the token is there."""
    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_API_TOKEN", "delivered-token")

    import ava.gateway_client.transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    client = cast(Any, gc._client_singleton())  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer delivered-token"


def test_gateway_client_in_an_agent_without_a_token_fails_instead_of_using_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.delenv("AVA_API_TOKEN", raising=False)

    import ava.gateway_client.transport as gc

    monkeypatch.setattr(gc, "_client", None)  # reset lazy singleton so it rebuilds
    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gc._client_singleton()  # pyright: ignore[reportUnknownMemberType]
