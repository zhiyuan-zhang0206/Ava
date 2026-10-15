from collections.abc import Callable
from typing import Any

import httpx
import pytest

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client import launch_retry, memory_search, transport
from base.agents.context.identity import AgentIdentity, ExternalLease


def owner() -> AvaContext:
    return AvaContext(
        clients=ClientSet(
            factories={
                transport.GatewayTransportInputs: lambda: transport.GatewayTransportInputs(
                    max_retries_reader=lambda: 1,
                    retry_delay_reader=lambda: 0.0,
                    memory_deadline_reader=lambda: 4.0,
                )
            }
        )
    )


def test_two_contexts_keep_clients_timeouts_and_wire_parameters_separate() -> None:
    first, second = owner(), owner()
    seen: list[tuple[str, str, str, dict[str, float | None]]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                str(request.url),
                request.content.decode(),
                request.extensions["timeout"],
            )
        )
        return httpx.Response(200, json={"host": request.url.host})

    try:
        with (
            httpx.Client(
                base_url="http://first", timeout=3, transport=httpx.MockTransport(answer)
            ) as a,
            httpx.Client(
                base_url="http://second", timeout=9, transport=httpx.MockTransport(answer)
            ) as b,
            first.clients.using_gateway(a),
            second.clients.using_gateway(b),
        ):
            assert transport.get("/api/read", params={"q": "first"}, context=first).json() == {
                "host": "first"
            }
            assert transport.post("/api/write", {"owner": "second"}, context=second).json() == {
                "host": "second"
            }
            assert transport.patch("/api/agents/42", {"label": "first"}, context=first).json() == {
                "host": "first"
            }
            transport.get("/api/override", timeout=httpx.Timeout(12), context=second)
            assert not a.is_closed and not b.is_closed
        assert a.is_closed and b.is_closed
    finally:
        first.clients.close()
        second.clients.close()
    assert [(method, url, body) for method, url, body, _timeout in seen] == [
        ("GET", "http://first/api/read?q=first", ""),
        ("POST", "http://second/api/write", '{"owner":"second"}'),
        ("PATCH", "http://first/api/agents/42", '{"label":"first"}'),
        ("GET", "http://second/api/override", ""),
    ]
    assert [timeout["read"] for _method, _url, _body, timeout in seen] == [3, 9, 3, 12]


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        (transport.get, ("/api/read",), {}),
        (transport.post, ("/api/write",), {}),
        (transport.patch, ("/api/agents/42",), {}),
        (memory_search.memory_search, ("query", 2), {"purpose": "search"}),
        (launch_retry.get_launch_attempt, (42,), {}),
        (
            launch_retry.retry_launch,
            (42,),
            {"idempotency_key": "key", "expected_prior_attempt_id": "invalid"},
        ),
    ],
)
def test_io_boundaries_require_a_context(
    operation: Callable[..., Any], args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    with pytest.raises(TypeError, match="context"):
        operation(*args, **kwargs)


def test_jitter_revalidates_borrowed_identity_and_propagates_expiration() -> None:
    checks: list[None] = []
    expired = False

    def validate() -> int:
        checks.append(None)
        if expired:
            raise RuntimeError("borrowed lease expired")
        return 321

    context = AvaContext(
        identity=AgentIdentity(
            agent_id=9,
            owns_loop=False,
            lease=ExternalLease(agent_id=321, validate=validate, config=lambda: None),
        )
    )
    assert transport.agent_jitter_seconds(context) == 1.605
    assert transport.agent_jitter_seconds(context) == 1.605
    assert len(checks) == 2
    expired = True
    with pytest.raises(RuntimeError, match="borrowed lease expired"):
        transport.agent_jitter_seconds(context)
    assert len(checks) == 3
