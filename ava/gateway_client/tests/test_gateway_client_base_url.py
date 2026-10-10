"""On an agent-runner (the conftest default role), the SDK client's base_url
must resolve to gateway_url, not the (empty/removed) gateway_url."""

from collections.abc import Iterator
from typing import Any, cast

import pytest

import ava
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.config import ConfigBoot
from tests.fixtures.configuration import snapshot_process_config


@pytest.fixture(autouse=True)
def client_config() -> Iterator[ConfigBoot]:
    """Each test builds the gateway client itself: a context with a clean `ClientSet`."""
    config = snapshot_process_config()
    context = AvaContext(clients=process_clients(config=config))
    ava.context = context
    try:
        yield config
    finally:
        del ava.context
        context.clients.close()


def test_gateway_client_base_url_is_gateway_url(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    from base.cluster.machine import reset_identity, set_identity

    set_identity(role="agent-runner")
    client_config.set_field("gateway_url", "https://cp.example.com")

    import ava.gateway_client.transport as gc

    try:
        client = cast(Any, gc._http())  # pyright: ignore[reportUnknownMemberType]
        assert str(client.base_url).rstrip("/") == "https://cp.example.com"
    finally:
        reset_identity()


def test_gateway_client_sends_cluster_secret_bearer(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """Regression: the gateway requires auth on every route. The SDK client must
    present the cluster secret as `Authorization: Bearer <secret>`, or every
    `ava.*` gateway call (list_agents / spawn / ...) 401s from inside an agent."""

    client_config.set_field("cluster_secret", "s3cr3t-token")

    import ava.gateway_client.transport as gc

    client = cast(Any, gc._http())  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer s3cr3t-token"


def test_gateway_client_no_bearer_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """An empty cluster secret (tests / unprovisioned checkout) sends no auth
    header — the gateway fails open in the same case, so the SDK must not send a
    bogus `Bearer ` either."""

    client_config.set_field("cluster_secret", "")

    import ava.gateway_client.transport as gc

    client = cast(Any, gc._http())  # pyright: ignore[reportUnknownMemberType]
    assert "Authorization" not in client.headers


def test_gateway_client_in_an_agent_presents_the_delivered_token(
    monkeypatch: pytest.MonkeyPatch,
    client_config: ConfigBoot,
) -> None:
    """An agent inherits its host's machine API token; the human secret never
    rides along when the token is there."""

    client_config.set_field("cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_API_TOKEN", "delivered-token")

    import ava.gateway_client.transport as gc

    client = cast(Any, gc._http())  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer delivered-token"


def test_gateway_client_in_an_agent_without_a_token_fails_instead_of_using_the_secret(
    monkeypatch: pytest.MonkeyPatch,
    client_config: ConfigBoot,
) -> None:

    client_config.set_field("cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.delenv("AVA_API_TOKEN", raising=False)

    import ava.gateway_client.transport as gc

    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gc._http()  # pyright: ignore[reportUnknownMemberType]


def _answering(label: str, seen: list[str]) -> Any:
    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(f"{label} {request.url}")
        return httpx.Response(200, json={})

    return httpx.Client(base_url=f"http://{label}.test", transport=httpx.MockTransport(answer))


def test_use_client_routes_sdk_calls_through_the_injected_client_and_restores_the_previous(
    monkeypatch: pytest.MonkeyPatch,
    client_config: ConfigBoot,
) -> None:
    import ava.gateway_client.transport as gc

    seen: list[str] = []
    outer = _answering("outer", seen)

    with gc.use_client(outer):
        with gc.use_client(_answering("injected", seen)) as injected:
            assert gc._http() is injected  # pyright: ignore[reportUnknownMemberType]
            gc.get("/api/x")  # pyright: ignore[reportUnknownMemberType]
        gc.get("/api/y")  # pyright: ignore[reportUnknownMemberType]

    assert seen == ["injected http://injected.test/api/x", "outer http://outer.test/api/y"]


def test_use_client_leaves_the_lazy_default_unbuilt_when_none_was_installed() -> None:
    import ava.gateway_client.transport as gc

    clients = ava.context.clients

    with pytest.raises(RuntimeError, match="boom"), gc.use_client(_answering("injected", [])):
        raise RuntimeError("boom")
    assert clients._gateway is None


def test_client_carries_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """The client default the sentinel defers to is the configured one.

    Without this, `USE_CLIENT_DEFAULT` could be deferring to httpx's own
    5s default rather than `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS`.
    """
    import ava.gateway_client.transport as gc

    client_config.set_field("gateway_client_http_timeout_seconds", 20.0)

    client = cast(Any, gc._http())  # pyright: ignore[reportUnknownMemberType]
    assert client.timeout.read == 20.0
    assert client.timeout.connect == 20.0


def test_explicit_host_context_routes_calls_without_a_local_sdk_binding() -> None:
    import httpx

    from ava.gateway_client import get_born_chain, memory_search
    from ava.gateway_client.transport import _agent_jitter_seconds
    from base.agents.context.identity import AgentIdentity
    from tests.fixtures.pin_agent import pin_no_identity

    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "ancestors": []})

    from ava.gateway_client.transport import GatewayTransportInputs
    from base.agents.context.clients import ClientSet

    context = AvaContext(
        identity=AgentIdentity(44, True),
        clients=ClientSet(
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: 3,
                    retry_delay_reader=lambda: 1.0,
                    memory_deadline_reader=lambda: 15.0,
                )
            }
        ),
    )
    pin_no_identity()
    with (
        httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(answer)) as client,
        context.clients.using_gateway(client),
    ):
        assert memory_search("query", 3, context=context) == []
        assert get_born_chain(44, context=context) == []
        assert _agent_jitter_seconds(context) == 0.22
    assert seen == [
        "http://host.test/api/memory/search",
        "http://host.test/api/agents/44/born-chain",
    ]
    assert getattr(ava, "context", None) is None


def test_explicit_host_transport_failure_keeps_its_own_client_and_error() -> None:
    import httpx

    from ava.gateway_client.transport import get
    from base.agents import GatewayUnavailable
    from tests.fixtures.pin_agent import pin_no_identity

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("expected outage", request=request)

    context = AvaContext()
    pin_no_identity()
    with (
        httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(fail)) as client,
        context.clients.using_gateway(client),
        pytest.raises(GatewayUnavailable, match=r"host\.test.*expected outage"),
    ):
        get("/api/check", max_retries=1, context=context)
    assert getattr(ava, "context", None) is None
