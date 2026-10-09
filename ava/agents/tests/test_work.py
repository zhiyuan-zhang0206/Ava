"""SDK work control keeps exact observed work, key and receipt boundaries."""

import contextlib
import io
import json
from uuid import uuid4

import httpx
import pytest

import ava
from ava.agents import work
from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from base.agents import GatewayUnavailable
from base.agents.incarnation.native_work_models import NativeWorkTarget

TARGET = NativeWorkTarget(
    work_id=uuid4(), agent_id=42, machine="local", generation=uuid4(), owner=uuid4(), protocol=1
)
COMMAND = uuid4()


def test_work_namespace_is_discoverable_in_sdk_help() -> None:
    assert "work" in ava.agents.__all_for_ava__
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        ava.help(work)
    assert all(
        name in output.getvalue() for name in ("observe", "cancel", "restart", "restart_status")
    )


@pytest.mark.parametrize("agent_id", [True, 0, -1, 2**63])
def test_invalid_observation_id_is_refused_before_http(agent_id: int) -> None:
    with pytest.raises(ValueError):
        work.observe(agent_id)


def test_observation_and_cancel_use_scoped_routes_and_exact_target() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = TARGET.model_dump(mode="json")
        if request.method == "POST":
            body = {"command_id": str(COMMAND), "target": body}
        return httpx.Response(200, json=body)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        target = work.observe(42)
        accepted = work.cancel(target, idempotency_key="intent")
        assert accepted.target == target and accepted.command_id == COMMAND
    assert calls[0].method == "GET" and calls[0].url.path == "/api/keyed/v1/agents/42/native-work"
    assert calls[1].url.path == "/api/keyed/v1/agents/42/cancel-work"
    assert calls[1].headers["Idempotency-Key"] == "intent"
    assert calls[1].headers["Idempotency-Scope"] == "principal-v1"
    assert json.loads(calls[1].content) == TARGET.model_dump(mode="json")


def test_lost_response_is_not_retried_and_caller_replay_keeps_original_target() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            200, json={"command_id": str(COMMAND), "target": TARGET.model_dump(mode="json")}
        )

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        with pytest.raises(GatewayUnavailable):
            work.cancel(TARGET, idempotency_key="intent")
        assert len(calls) == 1
        assert work.cancel(TARGET, idempotency_key="intent").command_id == COMMAND
    assert len(calls) == 2
    assert (
        calls[0].url == calls[1].url
        and calls[0].headers == calls[1].headers
        and calls[0].content == calls[1].content
    )


@pytest.mark.parametrize("status", [404, 409, 500])
def test_unsupported_or_refused_cancel_never_falls_back(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "refused"})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(httpx.HTTPStatusError),
    ):
        work.cancel(TARGET, idempotency_key="intent")
    assert len(calls) == 1 and calls[0].url.path.endswith("/cancel-work")


@pytest.mark.parametrize("key", ["", "x" * 129])
def test_invalid_key_refused_before_http(key: str) -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid request reached HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(unexpected)) as http,
        transport.use_client(http),
        pytest.raises(ValueError),
    ):
        work.cancel(TARGET, idempotency_key=key)


@pytest.mark.parametrize("field", ["protocol", "agent_id"])
def test_copied_invalid_target_is_revalidated_before_http(field: str) -> None:
    invalid = TARGET.model_copy(update={field: True})

    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid copied target reached HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(unexpected)) as http,
        transport.use_client(http),
        pytest.raises(ValueError),
    ):
        work.cancel(invalid, idempotency_key="intent")


def test_expired_external_identity_cannot_send(monkeypatch: pytest.MonkeyPatch) -> None:
    def expired() -> None:
        raise ValueError("lease expired")

    monkeypatch.setattr(agent_identity, "validate_external_identity", expired)
    with pytest.raises(ValueError, match="lease expired"):
        work.cancel(TARGET, idempotency_key="intent")


@pytest.mark.parametrize("operation", ["observe", "cancel"])
def test_mismatched_response_cannot_claim_acceptance(operation: str) -> None:
    other = TARGET.model_copy(update={"agent_id": 43})
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = other.model_dump(mode="json")
        if operation == "cancel":
            body = {"command_id": str(COMMAND), "target": body}
        return httpx.Response(200, json=body)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(ValueError, match="another"),
    ):
        if operation == "cancel":
            work.cancel(TARGET, idempotency_key="intent")
        else:
            work.observe(42)
    assert len(calls) == 1
