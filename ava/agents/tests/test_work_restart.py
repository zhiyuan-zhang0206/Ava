"""Observed restart keeps original work, caller attribution and command progress."""

import json
from uuid import uuid4

import httpx
import pytest

from ava.agents import work
from ava.agents.tests.test_work import TARGET
from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from base.agents import GatewayUnavailable
from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartOutcome,
    NativeRestartProgress,
)
from tests.fixtures.pin_agent import pin_agent

ACCEPTED = NativeRestartAcceptance(
    command_id=123, target=TARGET, config_overlay={"completion_notice_policy": "hourly"}
)
PROGRESS = NativeRestartProgress(
    acceptance=ACCEPTED,
    outcome=NativeRestartOutcome.ACCEPTED,
    applied_at=None,
    observed_at=None,
    reason=None,
)


def test_restart_and_status_use_exact_guarded_command() -> None:
    pin_agent(7)
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json=(ACCEPTED if len(calls) == 1 else PROGRESS).model_dump(mode="json")
        )

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        accepted = work.restart(
            TARGET, idempotency_key="intent", config_overlay=ACCEPTED.config_overlay
        )
        assert work.restart_status(accepted).outcome == NativeRestartOutcome.ACCEPTED
    assert calls[0].url.path == "/api/keyed/v1/agents/42/restart-work"
    assert calls[1].url.path == "/api/keyed/v1/agents/42/restart-commands/123"
    assert json.loads(calls[0].content) == {
        "target": TARGET.model_dump(mode="json"),
        "source": "agent:7",
        "config_overlay": ACCEPTED.config_overlay,
    }
    assert (
        calls[0].headers["Idempotency-Key"] == "intent"
        and calls[0].headers["Idempotency-Scope"] == "principal-v1"
    )


def test_restart_requires_explicit_replay_after_lost_response() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=ACCEPTED.model_dump(mode="json"))

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        with pytest.raises(GatewayUnavailable):
            work.restart(TARGET, idempotency_key="intent", config_overlay=ACCEPTED.config_overlay)
        assert len(calls) == 1
        assert (
            work.restart(TARGET, idempotency_key="intent", config_overlay=ACCEPTED.config_overlay)
            == ACCEPTED
        )
    assert (
        calls[0].url == calls[1].url
        and calls[0].headers == calls[1].headers
        and calls[0].content == calls[1].content
    )


@pytest.mark.parametrize("status", [404, 409, 422, 500, 502])
def test_refused_or_unsupported_restart_never_falls_back(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "refused"})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(httpx.HTTPStatusError),
    ):
        work.restart(TARGET, idempotency_key="intent")
    assert len(calls) == 1 and calls[0].url.path.endswith("/restart-work")


@pytest.mark.parametrize("operation", ["restart", "status"])
def test_another_acceptance_is_rejected(operation: str) -> None:
    other = ACCEPTED.model_copy(update={"target": TARGET.model_copy(update={"work_id": uuid4()})})
    body = other if operation == "restart" else PROGRESS.model_copy(update={"acceptance": other})
    with (
        httpx.Client(
            base_url="http://gateway",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=body.model_dump(mode="json"))
            ),
        ) as http,
        transport.use_client(http),
        pytest.raises(ValueError, match="another"),
    ):
        if operation == "restart":
            work.restart(TARGET, idempotency_key="intent")
        else:
            work.restart_status(ACCEPTED)


def test_non_finite_overlay_is_rejected_before_http() -> None:
    with pytest.raises(ValueError):
        work.restart(TARGET, idempotency_key="intent", config_overlay={"value": float("nan")})


@pytest.mark.parametrize("operation", ["restart", "status"])
def test_invalid_copied_identity_is_revalidated_before_http(operation: str) -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid copied identity reached HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(unexpected)) as http,
        transport.use_client(http),
        pytest.raises(ValueError),
    ):
        if operation == "restart":
            work.restart(TARGET.model_copy(update={"agent_id": True}), idempotency_key="intent")
        else:
            work.restart_status(ACCEPTED.model_copy(update={"command_id": True}))


@pytest.mark.parametrize("operation", ["restart", "status"])
def test_expired_external_identity_cannot_send(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def expired() -> None:
        raise ValueError("lease expired")

    monkeypatch.setattr(agent_identity, "validate_external_identity", expired)
    with pytest.raises(ValueError, match="lease expired"):
        if operation == "restart":
            work.restart(TARGET, idempotency_key="intent")
        else:
            work.restart_status(ACCEPTED)


def test_restart_without_identity_fails_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_agent(None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    monkeypatch.delenv("AVA_CALLER_IDENTITY", raising=False)
    calls: list[httpx.Request] = []

    def unexpected(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        pytest.fail("restart without identity reached HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(unexpected)) as http,
        transport.use_client(http),
        pytest.raises(RuntimeError, match="no established actor or agent identity"),
    ):
        work.restart(TARGET, idempotency_key="intent")
    assert calls == []


def test_restart_stamps_system_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_agent(None, actor="system:test-restart")
    monkeypatch.delenv("AVA_CALLER_IDENTITY", raising=False)
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=ACCEPTED.model_dump(mode="json"))

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        assert work.restart(TARGET, idempotency_key="intent") == ACCEPTED
    assert len(calls) == 1
    assert json.loads(calls[0].content)["source"] == "system:test-restart"
