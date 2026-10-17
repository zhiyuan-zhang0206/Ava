"""On an agent-runner (the conftest default role), the SDK client's base_url
must resolve to gateway_url, not the (empty/removed) gateway_url."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import meiva
from base.agents.context import MeivaContext
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from meiva.boot import process_clients
from meiva.gateway_client import use_client
from meiva.sdk_surface import process_context


@pytest.fixture(autouse=True)
def client_config(machine_identity: MachineIdentity) -> Iterator[ConfigBoot]:
    """Each test builds the gateway client itself: a context with a clean `ClientSet`."""
    config = snapshot_process_config()
    context = MeivaContext(
        machine=machine_identity,
        clients=process_clients(config=config, machine=machine_identity),
    )
    meiva.context = context
    try:
        yield config
    finally:
        del meiva.context
        context.clients.close()


def test_gateway_client_base_url_is_gateway_url(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    meiva.context.require_machine().set(role="agent-runner")
    client_config.set_field("gateway_url", "https://cp.example.com")

    import base.agents.context.gateway_client.transport as gc

    client = cast(Any, gc.gateway_http(context=process_context.current_context()))  # pyright: ignore[reportUnknownMemberType]
    assert str(client.base_url).rstrip("/") == "https://cp.example.com"


def test_gateway_client_sends_cluster_secret_bearer(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """Regression: the gateway requires auth on every route. The SDK client must
    present the cluster secret as `Authorization: Bearer <secret>`, or every
    `meiva.*` gateway call (list_agents / spawn / ...) 401s from inside an agent."""

    client_config.set_field("cluster_secret", "s3cr3t-token")

    import base.agents.context.gateway_client.transport as gc

    client = cast(Any, gc.gateway_http(context=process_context.current_context()))  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer s3cr3t-token"


def test_gateway_client_without_any_credential_fails_instead_of_sending_none(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """The gateway always authenticates: a process holding neither a machine token nor
    the cluster secret is told so, rather than sending an anonymous (or bogus
    `Bearer `) request that would only come back 401."""

    client_config.set_field("cluster_secret", "")
    monkeypatch.delenv("MEIVA_API_TOKEN", raising=False)

    import base.agents.context.gateway_client.transport as gc
    from base.cluster.machine import GatewayApiTokenMissing

    with pytest.raises(GatewayApiTokenMissing):
        gc.gateway_http(context=process_context.current_context())  # pyright: ignore[reportUnknownMemberType]


def test_gateway_client_in_an_agent_presents_the_delivered_token(
    monkeypatch: pytest.MonkeyPatch,
    client_config: ConfigBoot,
) -> None:
    """An agent inherits its host's machine API token; the human secret never
    rides along when the token is there."""

    client_config.set_field("cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("MEIVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("MEIVA_API_TOKEN", "delivered-token")

    import base.agents.context.gateway_client.transport as gc

    client = cast(Any, gc.gateway_http(context=process_context.current_context()))  # pyright: ignore[reportUnknownMemberType]
    assert client.headers.get("Authorization") == "Bearer delivered-token"


def test_gateway_client_in_an_agent_without_a_token_fails_instead_of_using_the_secret(
    monkeypatch: pytest.MonkeyPatch,
    client_config: ConfigBoot,
) -> None:

    client_config.set_field("cluster_secret", "s3cr3t-token")
    monkeypatch.setenv("MEIVA_PROCESS_PROFILE", "agent")
    monkeypatch.delenv("MEIVA_API_TOKEN", raising=False)

    import base.agents.context.gateway_client.transport as gc

    with pytest.raises(RuntimeError, match="MEIVA_API_TOKEN"):
        gc.gateway_http(context=process_context.current_context())  # pyright: ignore[reportUnknownMemberType]


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
    import base.agents.context.gateway_client.transport as gc

    seen: list[str] = []
    outer = _answering("outer", seen)

    with use_client(outer):
        with use_client(_answering("injected", seen)) as injected:
            assert gc.gateway_http(context=process_context.current_context()) is injected  # pyright: ignore[reportUnknownMemberType]
            gc.get("/api/x", context=process_context.current_context())  # pyright: ignore[reportUnknownMemberType]
        gc.get("/api/y", context=process_context.current_context())  # pyright: ignore[reportUnknownMemberType]

    assert seen == ["injected http://injected.test/api/x", "outer http://outer.test/api/y"]


def test_use_client_leaves_the_lazy_default_unbuilt_when_none_was_installed() -> None:

    clients = meiva.context.clients

    with pytest.raises(RuntimeError, match="boom"), use_client(_answering("injected", [])):
        raise RuntimeError("boom")
    assert clients._gateway is None


def test_client_carries_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch, client_config: ConfigBoot
) -> None:
    """The client default the sentinel defers to is the configured one.

    Without this, `USE_CLIENT_DEFAULT` could be deferring to httpx's own
    5s default rather than `MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS`.
    """
    import base.agents.context.gateway_client.transport as gc

    client_config.set_field("gateway_client_http_timeout_seconds", 20.0)

    client = cast(Any, gc.gateway_http(context=process_context.current_context()))  # pyright: ignore[reportUnknownMemberType]
    assert client.timeout.read == 20.0
    assert client.timeout.connect == 20.0


def test_explicit_host_context_routes_calls_without_a_local_sdk_binding() -> None:
    import httpx

    from base.agents.context.gateway_client.memory_search import memory_search
    from base.agents.context.gateway_client.transport import agent_jitter_seconds
    from base.agents.context.identity import AgentIdentity
    from meiva.gateway_client import get_born_chain
    from meiva.tests.fixtures.pin_agent import pin_no_identity

    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "ancestors": []})

    from base.agents.context.clients import ClientSet
    from base.agents.context.gateway_client.transport import GatewayTransportInputs

    pin_no_identity()
    with httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(answer)) as client:
        context = MeivaContext(
            identity=AgentIdentity(44, True),
            clients=ClientSet(
                gateway_url="http://host.test",
                gateway=lambda _url: client,
                factories={
                    GatewayTransportInputs: lambda: GatewayTransportInputs(
                        max_retries_reader=lambda: 3,
                        retry_delay_reader=lambda: 1.0,
                        memory_deadline_reader=lambda: 15.0,
                    )
                },
            ),
        )
        assert memory_search("query", 3, purpose="search", context=context) == []
        assert get_born_chain(44, context=context) == []
        assert agent_jitter_seconds(context) == 0.22
    assert seen == [
        "http://host.test/api/memory/search",
        "http://host.test/api/agents/44/born-chain",
    ]
    assert getattr(meiva, "context", None) is None


def test_explicit_host_transport_failure_keeps_its_own_client_and_error() -> None:
    import httpx

    from base.agents.context.gateway_client.transport import get
    from base.api_contracts.agent_contract import GatewayUnavailable
    from meiva.tests.fixtures.pin_agent import pin_no_identity

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("expected outage", request=request)

    from base.agents.context.clients import ClientSet

    pin_no_identity()
    with (
        httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(fail)) as client,
        pytest.raises(GatewayUnavailable, match=r"host\.test.*expected outage"),
    ):
        context = MeivaContext(
            clients=ClientSet(gateway_url="http://host.test", gateway=lambda _url: client)
        )
        get("/api/check", max_retries=1, context=context)
    assert getattr(meiva, "context", None) is None


def test_get_adapter_keeps_explicit_context_ahead_of_the_sdk_binding() -> None:
    import httpx

    from base.agents.context.clients import ClientSet
    from base.agents.context.gateway_client.transport import GatewayTransportInputs
    from meiva.gateway_client import get, use_client

    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"host": request.url.host})

    with (
        httpx.Client(
            base_url="http://bound", timeout=5, transport=httpx.MockTransport(answer)
        ) as bound,
        httpx.Client(
            base_url="http://explicit", timeout=8, transport=httpx.MockTransport(answer)
        ) as supplied,
        use_client(bound),
    ):
        explicit = MeivaContext(
            clients=ClientSet(
                gateway_url="http://explicit",
                gateway=lambda _url: supplied,
                factories={
                    GatewayTransportInputs: lambda: GatewayTransportInputs(
                        max_retries_reader=lambda: 1,
                        retry_delay_reader=lambda: 0.0,
                        memory_deadline_reader=lambda: 1.0,
                    )
                },
            )
        )
        try:
            assert get("/api/read", params={"q": "bound"}).json() == {"host": "bound"}
            assert get(
                "/api/read", params={"q": "supplied"}, timeout=httpx.Timeout(13), context=explicit
            ).json() == {"host": "explicit"}
        finally:
            explicit.clients.close()
    assert [str(request.url) for request in seen] == [
        "http://bound/api/read?q=bound",
        "http://explicit/api/read?q=supplied",
    ]
    assert [request.extensions["timeout"]["read"] for request in seen] == [5, 13]


def test_get_adapter_refuses_ambient_shared_host_but_accepts_its_explicit_context(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import sys

    env = {key: value for key, value in os.environ.items() if key != "MEIVA_AGENT_ID"}
    env.update(MEIVA_HOME=str(tmp_path), MEIVA_CONFIG_FETCH="skip")
    result = subprocess.run(  # noqa: S603 — fixed interpreter and local contract probe
        [sys.executable, "-I", str(Path(__file__).with_name("child_get_context.py"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
