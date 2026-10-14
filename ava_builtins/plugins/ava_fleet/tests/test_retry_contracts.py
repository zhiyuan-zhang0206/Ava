"""Unprotected writes must not be repeated after an ambiguous outcome."""

import json

import httpx
import pytest

from ava.gateway_client import transport
from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.cluster.machine.tests.identity import set_machine_identity as set_machine_identity
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.model_owner import model_installation as model_installation
from tests.fixtures.unit.model_owner import sdk_model_owner as sdk_model_owner
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/agents/7/restart"),
        ("POST", "/api/schedules/1/restart"),
        ("PATCH", "/api/unknown"),
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

        def send() -> transport.TransportResponse:
            if method == "POST":
                return transport.post(path)
            return transport.patch(path)

        if failure == "timeout":
            with pytest.raises(GatewayUnavailable, match="result unknown"):
                send()
        else:
            assert send().status_code == 503
    assert len(requests) == 1


@pytest.mark.usefixtures("retry_waits")
def test_natural_idempotent_write_retries() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway") as client,
        transport.use_client(client),
    ):
        response = transport.patch("/api/agents/1", {"label": "new"})
    assert response.status_code == 200
    assert len(requests) == 2


def _lost_response_handler(failure: str, requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(503)

    return handler


def _assert_one_logical_key(requests: list[httpx.Request], key: str | None = None) -> None:
    """Every attempt of one keyed call repeats the same key and principal scope."""
    assert len(requests) > 1
    keys = {request.headers["Idempotency-Key"] for request in requests}
    assert len(keys) == 1
    assert key is None or keys == {key}
    assert {request.headers["Idempotency-Scope"] for request in requests} == {"principal-v1"}


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
@pytest.mark.usefixtures("retry_waits")
def test_keyed_route_retries_an_ambiguous_outcome_under_one_key(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from base.api_contracts import contracts
    from base.api_contracts.contracts import Idempotency, RouteContract

    path = "/api/new-keyed-effect"
    monkeypatch.setitem(
        contracts.ROUTE_CONTRACTS,
        ("POST", path),
        RouteContract(Idempotency.AT_LEAST_ONCE_WITH_KEY),
    )
    requests: list[httpx.Request] = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(_lost_response_handler(failure, requests)),
            base_url="http://gateway",
        ) as client,
        transport.use_client(client),
    ):
        if failure == "timeout":
            with pytest.raises(GatewayUnavailable):
                transport.post(path, idempotency_key="intent-1")
        else:
            assert transport.post(path, idempotency_key="intent-1").status_code == 503
    _assert_one_logical_key(requests, "intent-1")


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
@pytest.mark.usefixtures("retry_waits")
def test_keyed_patch_retries_an_ambiguous_outcome_under_the_caller_key(failure: str) -> None:
    requests: list[httpx.Request] = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(_lost_response_handler(failure, requests)),
            base_url="http://gateway",
        ) as client,
        transport.use_client(client),
    ):
        if failure == "timeout":
            with pytest.raises(GatewayUnavailable):
                transport.patch("/api/tasks/1", {"priority": "P1"}, idempotency_key="original")
        else:
            response = transport.patch(
                "/api/tasks/1", {"priority": "P1"}, idempotency_key="original"
            )
            assert response.status_code == 503
    _assert_one_logical_key(requests, "original")


def test_keyed_post_and_patch_mint_a_key_when_none_is_pinned() -> None:
    requests: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with (
        httpx.Client(transport=httpx.MockTransport(accept), base_url="http://gateway") as client,
        transport.use_client(client),
    ):
        transport.patch("/api/tasks/1", {"priority": "P1"})
        transport.post("/api/agents/1/system-note", {})
    for request in requests:
        assert request.headers["Idempotency-Key"]
        assert request.headers["Idempotency-Scope"] == "principal-v1"


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


@pytest.mark.parametrize("operation", ["register", "close"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
@pytest.mark.usefixtures("retry_waits")
def test_public_page_call_retries_an_ambiguous_effect_under_one_key(
    operation: str, failure: str
) -> None:
    from ava.gateway_client import close_page, register_page

    writes: list[httpx.Request] = []

    def committed_then_response_failed(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 5, "name": "report"}])
        # A real gateway may already have replaced/closed the page; the keyed
        # route replays that original acceptance instead of acting again.
        writes.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("page effect committed; response lost", request=request)
        return httpx.Response(503)

    with (
        httpx.Client(
            transport=httpx.MockTransport(committed_then_response_failed), base_url="http://gateway"
        ) as client,
        transport.use_client(client),
    ):
        expected_error = GatewayUnavailable if failure == "timeout" else httpx.HTTPStatusError
        with pytest.raises(expected_error):
            if operation == "register":
                register_page(7, name="report", port=8001, host="test-host", title=None)
            else:
                close_page(7, "report")
    _assert_one_logical_key(writes)
    assert {write.method for write in writes} == {"POST"}
    assert {write.url.path for write in writes} == {
        "/api/keyed/v1/agents/7/pages"
        if operation == "register"
        else "/api/keyed/v1/agents/7/pages/report/close"
    }
    if operation == "close":
        assert {json.loads(write.content)["expected_page_id"] for write in writes} == {5}


def test_close_page_without_an_open_page_sends_no_write() -> None:
    from ava.gateway_client import close_page

    requests: list[httpx.Request] = []

    def only_reads(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    with (
        httpx.Client(
            transport=httpx.MockTransport(only_reads), base_url="http://gateway"
        ) as client,
        transport.use_client(client),
    ):
        assert close_page(7, "report") is False
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("operation", ["edit", "dismiss"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
@pytest.mark.usefixtures("retry_waits")
def test_fleet_current_notice_call_retries_its_observed_intent_under_one_key(
    operation: str, failure: str
) -> None:
    from ava_builtins.plugins.ava_fleet.plugin import dismiss_notice, edit_notice
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(7)
    writes: list[httpx.Request] = []

    def response_lost(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 11}])
        writes.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
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
    _assert_one_logical_key(writes)
    assert {write.method for write in writes} == {"PATCH" if operation == "edit" else "POST"}
    assert {write.url.path for write in writes} == {
        "/api/agents/7/notices/current"
        + ("/guarded-v1" if operation == "edit" else "/dismiss/guarded-v1")
    }
    assert {json.loads(write.content)["observed_notice_id"] for write in writes} == {11}


@pytest.mark.parametrize("operation", ["edit", "dismiss"])
def test_fleet_current_notice_call_without_an_open_notice_sends_no_write(operation: str) -> None:
    from ava_builtins.plugins.ava_fleet.plugin import dismiss_notice, edit_notice
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(7)
    requests: list[httpx.Request] = []

    def only_reads(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    with (
        httpx.Client(
            transport=httpx.MockTransport(only_reads), base_url="http://gateway"
        ) as client,
        transport.use_client(client),
    ):
        if operation == "edit":
            edit_notice(title="Nothing open")
        else:
            dismiss_notice()
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.params["agent_id"] == "7"


def test_keyed_patch_sends_the_principal_scope_with_the_caller_key() -> None:
    requests: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with (
        httpx.Client(transport=httpx.MockTransport(accept), base_url="http://gateway") as client,
        transport.use_client(client),
    ):
        transport.patch(
            "/api/agents/7/notices/current/guarded-v1",
            {"observed_notice_id": 3, "title": "x"},
            idempotency_key="intent-1",
        )
    assert requests[0].headers["Idempotency-Key"] == "intent-1"
    assert requests[0].headers["Idempotency-Scope"] == "principal-v1"
