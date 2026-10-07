"""Unprotected writes must not be repeated after an ambiguous outcome."""

import httpx
import pytest

from ava.gateway_client import transport
from base.agents import GatewayUnavailable


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/cancel"),
        ("POST", "/api/agents"),
        ("POST", "/api/agents/7/restart"),
        ("POST", "/api/agents/7/compact"),
        ("POST", "/api/agents/7/notices/1/resolve"),
        ("POST", "/api/agents/7/notices/current/dismiss"),
        ("POST", "/api/schedules/1/restart"),
        ("PATCH", "/api/tasks/1"),
        ("PATCH", "/api/agents/7/notices/current"),
        ("PATCH", "/api/unknown"),
        ("DELETE", "/api/cluster/machines/host"),
        ("DELETE", "/api/unknown"),
    ],
)
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_unprotected_write_is_sent_once(method: str, path: str, failure: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        transport.use_client(client),
    ):

        def send() -> httpx.Response:
            if method == "POST":
                return transport.post(path)
            if method == "PATCH":
                return transport.patch(path)
            return transport._delete(path)

        if failure == "timeout":
            with pytest.raises(GatewayUnavailable, match="result unknown"):
                send()
        else:
            assert send().status_code == 503
    assert len(requests) == 1
    if path in ("/api/agents", "/api/cancel", "/api/agents/7/compact", "/api/tasks/1"):
        assert requests[0].headers["Idempotency-Key"]


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.usefixtures("retry_waits")
def test_natural_idempotent_write_retries(method: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        transport.use_client(client),
    ):
        response = (
            transport.patch("/api/agents/1", {"label": "new"})
            if method == "PATCH"
            else transport._delete("/api/agents/1/pages/report")
        )
    assert response.status_code == 200
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_new_keyed_route_does_not_activate_ambiguous_retry(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from base.api_contracts import contracts
    from base.api_contracts.contracts import Idempotency, RouteContract

    path = "/api/new-keyed-effect"
    monkeypatch.setitem(
        contracts.ROUTE_CONTRACTS,
        ("POST", path),
        RouteContract(Idempotency.AT_LEAST_ONCE_WITH_KEY, transactional_idempotency=True),
    )
    requests: list[httpx.Request] = []

    def older_gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("older server ignores key", request=request)
        return httpx.Response(503)

    with (
        httpx.Client(
            transport=httpx.MockTransport(older_gateway), base_url="http://gateway"
        ) as client,
        transport.use_client(client),
    ):
        if failure == "timeout":
            with pytest.raises(GatewayUnavailable, match="result unknown"):
                transport.post(path, idempotency_key="intent-1")
        else:
            assert transport.post(path, idempotency_key="intent-1").status_code == 503
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "intent-1"


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_task_patch_caller_key_survives_explicit_replay(failure: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("old gateway ignored key", request=request)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        transport.use_client(client),
    ):
        for _ in range(2):
            if failure == "timeout":
                with pytest.raises(GatewayUnavailable, match="result unknown"):
                    transport.patch("/api/tasks/1", {"priority": "P1"}, idempotency_key="original")
            else:
                assert (
                    transport.patch(
                        "/api/tasks/1", {"priority": "P1"}, idempotency_key="original"
                    ).status_code
                    == 503
                )
    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == ["original", "original"]


@pytest.mark.parametrize("key", ["", "x" * 129, 17])
def test_task_patch_rejects_invalid_caller_key_before_network(key: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        transport.use_client(client),
        pytest.raises((TypeError, ValueError), match="idempotency key"),
    ):
        transport.patch("/api/tasks/1", idempotency_key=key)  # type: ignore[arg-type]
    assert requests == []


@pytest.mark.parametrize("operation", ["edit", "dismiss"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
@pytest.mark.usefixtures("retry_waits")
def test_fleet_current_notice_call_does_not_repeat_old_intent(operation: str, failure: str) -> None:
    from ava_builtins.plugins.ava_fleet.plugin import dismiss_notice, edit_notice
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(7)
    requests: list[httpx.Request] = []

    def response_lost(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("old notice changed; response lost", request=request)
        return httpx.Response(503)

    with (
        httpx.Client(
            transport=httpx.MockTransport(response_lost), base_url="http://gateway"
        ) as client,
        transport.use_client(client),
    ):
        error = GatewayUnavailable if failure == "timeout" else httpx.HTTPStatusError
        with pytest.raises(error):
            if operation == "edit":
                edit_notice(title="Original edit")
            else:
                dismiss_notice()
    assert len(requests) == 1
    assert requests[0].method == ("PATCH" if operation == "edit" else "POST")
    assert "Idempotency-Key" not in requests[0].headers
