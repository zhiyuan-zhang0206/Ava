"""Guarded SDK launch retries keep observed intent and refuse uncertain receipts."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ava import agents, gateway_client
from ava.gateway_client import transport
from base.agents import GatewayUnavailable

PRIOR = uuid4()
ATTEMPT = uuid4()


def receipt(**changes: Any) -> dict[str, Any]:
    return {
        "agent_id": 42,
        "prior_attempt_id": str(PRIOR),
        "launch_attempt_id": str(ATTEMPT),
        "accepted_at": "2026-10-09T00:00:00Z",
        "accepted": True,
        "execution_observed": False,
        **changes,
    }


@pytest.fixture
def requests() -> Iterator[list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=receipt())

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        yield calls


def submit(**changes: Any) -> int:
    arguments: dict[str, Any] = {
        "require_idempotency": True,
        "idempotency_key": "retry-intent",
        "expected_prior_attempt_id": PRIOR,
    }
    return agents.retry_launch(42, **(arguments | changes))


def test_sdk_uses_fixed_path_key_and_observed_body(requests: list[httpx.Request]) -> None:
    assert submit() == 42
    assert submit(expected_prior_attempt_id=str(PRIOR)) == 42
    assert len(requests) == 2
    for request in requests:
        assert request.url.path == "/api/keyed/v1/agents/42/retry-launch"
        assert request.headers["Idempotency-Key"] == "retry-intent"
        assert request.headers["Idempotency-Scope"] == "principal-v1"
        assert json.loads(request.content) == {"expected_prior_attempt_id": str(PRIOR)}


@pytest.mark.parametrize("boundary", [agents.retry_launch, gateway_client.retry_launch])
@pytest.mark.parametrize(
    "changes",
    [
        {"require_idempotency": 1},
        {"idempotency_key": None},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
        {"expected_prior_attempt_id": None},
        {"expected_prior_attempt_id": True},
        {"expected_prior_attempt_id": "invalid"},
        {"require_idempotency": False},
    ],
)
def test_invalid_admission_has_no_http(
    requests: list[httpx.Request], boundary: Any, changes: dict[str, Any]
) -> None:
    args = {
        "require_idempotency": True,
        "idempotency_key": "retry-intent",
        "expected_prior_attempt_id": PRIOR,
    }
    with pytest.raises((TypeError, ValueError)):
        boundary(42, **(args | changes))
    assert requests == []


@pytest.mark.parametrize("agent_id", [True, 0, -1, 2**63])
def test_guarded_client_refuses_invalid_agent_ids(
    requests: list[httpx.Request], agent_id: int
) -> None:
    with pytest.raises((TypeError, ValueError)):
        gateway_client.retry_launch(
            agent_id,
            require_idempotency=True,
            idempotency_key="intent",
            expected_prior_attempt_id=PRIOR,
        )
    assert requests == []


@pytest.mark.parametrize("status", [404, 409, 500])
def test_http_failure_never_downgrades_or_retries(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "refused"})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(httpx.HTTPStatusError),
    ):
        submit()
    assert len(calls) == 1
    assert calls[0].url.path == "/api/keyed/v1/agents/42/retry-launch"


def test_response_loss_is_one_attempt_and_explicit_replay_keeps_identity() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("accepted response lost", request=request)
        return httpx.Response(200, json=receipt())

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        with pytest.raises(GatewayUnavailable):
            submit()
        assert len(calls) == 1
        assert submit() == 42
    assert len(calls) == 2
    assert calls[0].url == calls[1].url
    assert calls[0].headers == calls[1].headers
    assert calls[0].content == calls[1].content


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 42},
        receipt(agent_id=43),
        receipt(agent_id=True),
        receipt(accepted=1),
        receipt(execution_observed=0),
        receipt(prior_attempt_id=str(uuid4())),
        receipt(launch_attempt_id=str(PRIOR)),
        receipt(launch_attempt_id="invalid"),
        {k: v for k, v in receipt().items() if k != "accepted"},
    ],
)
def test_malformed_acceptance_cannot_report_success(bad: dict[str, Any]) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=bad)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(ValueError),
    ):
        submit()
    assert len(calls) == 1


def test_legacy_sdk_keeps_its_original_endpoint() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": 42})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        assert agents.retry_launch(42) == 42
    assert len(calls) == 1
    assert calls[0].url.path == "/api/agents/42/retry-launch"
    assert "Idempotency-Scope" not in calls[0].headers


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"last_launch_attempt_id": None},
        {"last_launch_attempt_id": True},
        {"last_launch_attempt_id": "invalid"},
    ],
)
def test_missing_or_invalid_observation_cannot_authorize_a_retry(observed: dict[str, Any]) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=observed)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises((TypeError, ValueError)),
    ):
        agents.get_launch_attempt(42)
    assert len(calls) == 1 and calls[0].method == "GET"
