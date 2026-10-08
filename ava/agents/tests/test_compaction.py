"""Manual compaction SDK validates exact source, receipt, status and retry inputs."""

import contextlib
import io
import json
from uuid import uuid4

import httpx
import pytest

import ava
from ava.agents import compaction
from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from base.agents import GatewayUnavailable
from base.agents.compaction.models import (
    CompactAcceptance,
    CompactOutcome,
    CompactStatus,
    CompactTarget,
)
from base.agents.incarnation.native_work_models import NativeWorkTarget

TARGET = CompactTarget(
    protocol=1,
    observation_id=uuid4(),
    source=NativeWorkTarget(
        work_id=uuid4(), agent_id=42, machine="local", generation=uuid4(), owner=uuid4(), protocol=1
    ),
    checkpoint_id="original",
    checkpoint_ns="",
    messages_version="1",
    compact_channel_version=None,
    segment_version=0,
    model="gpt-6.1-sol",
)
ACCEPTED = CompactAcceptance(command_id=uuid4(), target=TARGET)
STATUS = CompactStatus(
    acceptance=ACCEPTED,
    outcome=CompactOutcome.ACCEPTED,
    reason=None,
    checkpoint_id=None,
    recovery_checkpoint_id=None,
    attempt_id=None,
    attempt_provider=None,
    execution=None,
    result_available=False,
    continuation_released=False,
)


def test_compaction_namespace_is_discoverable_in_sdk_help() -> None:
    assert "compaction" in ava.agents.__all_for_ava__
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        ava.help(compaction)
    assert all(name in output.getvalue() for name in ("observe", "submit", "status"))


def test_fixed_routes_keep_original_source_and_distinguish_status() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = TARGET if len(calls) == 1 else ACCEPTED if len(calls) == 2 else STATUS
        return httpx.Response(202 if len(calls) == 2 else 200, json=body.model_dump(mode="json"))

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        target = compaction.observe(42)
        accepted = compaction.submit(target, idempotency_key="intent")
        current = compaction.status(accepted)
    assert current.outcome == "accepted" and not current.continuation_released
    assert calls[0].url.path == "/api/keyed/v1/agents/42/compact-target"
    assert calls[1].url.path == "/api/keyed/v1/agents/42/compact-history"
    assert calls[2].url.path == f"/api/keyed/v1/agents/42/compact-commands/{ACCEPTED.command_id}"
    assert calls[1].headers["Idempotency-Key"] == "intent"
    assert calls[1].headers["Idempotency-Scope"] == "principal-v1"
    assert json.loads(calls[1].content) == TARGET.model_dump(mode="json")


def test_lost_response_requires_explicit_same_source_replay() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(202, json=ACCEPTED.model_dump(mode="json"))

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        with pytest.raises(GatewayUnavailable):
            compaction.submit(TARGET, idempotency_key="intent")
        assert len(calls) == 1
        assert compaction.submit(TARGET, idempotency_key="intent") == ACCEPTED
    assert (
        calls[0].url == calls[1].url
        and calls[0].headers == calls[1].headers
        and calls[0].content == calls[1].content
    )


@pytest.mark.parametrize("status", [404, 409, 500])
def test_refusal_never_falls_back_or_retries(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "refused"})

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
        pytest.raises(httpx.HTTPStatusError),
    ):
        compaction.submit(TARGET, idempotency_key="intent")
    assert len(calls) == 1 and calls[0].url.path.endswith("/compact-history")


@pytest.mark.parametrize("operation", ["observe", "submit", "status"])
def test_mismatched_response_fails_closed(operation: str) -> None:
    other = TARGET.model_copy(
        update={
            "observation_id": uuid4(),
            "source": TARGET.source.model_copy(update={"agent_id": 43}),
        }
    )
    accepted = ACCEPTED.model_copy(update={"target": other})
    response = (
        other
        if operation == "observe"
        else accepted
        if operation == "submit"
        else STATUS.model_copy(update={"acceptance": accepted})
    )
    with (
        httpx.Client(
            base_url="http://gateway",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    202 if operation == "submit" else 200, json=response.model_dump(mode="json")
                )
            ),
        ) as http,
        transport.use_client(http),
        pytest.raises(ValueError, match="another"),
    ):
        if operation == "observe":
            compaction.observe(42)
        elif operation == "submit":
            compaction.submit(TARGET, idempotency_key="intent")
        else:
            compaction.status(ACCEPTED)


@pytest.mark.parametrize("agent_id", [True, 0, -1, 2**63])
def test_invalid_observation_id_precedes_http(agent_id: int) -> None:
    with pytest.raises(ValueError):
        compaction.observe(agent_id)


@pytest.mark.parametrize("field", ["protocol", "segment_version", "source_agent"])
def test_copied_invalid_target_is_revalidated_before_http(field: str) -> None:
    changes = (
        {field: True}
        if field != "source_agent"
        else {"source": TARGET.source.model_copy(update={"agent_id": True})}
    )

    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid copied target reached HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(unexpected)) as http,
        transport.use_client(http),
        pytest.raises(ValueError),
    ):
        compaction.submit(TARGET.model_copy(update=changes), idempotency_key="intent")


@pytest.mark.parametrize("key", ["", "x" * 129])
def test_invalid_key_precedes_http(key: str) -> None:
    with pytest.raises(ValueError):
        compaction.submit(TARGET, idempotency_key=key)


@pytest.mark.parametrize("operation", ["observe", "submit", "status"])
def test_attached_identity_is_rechecked_before_http(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def expired() -> None:
        raise ValueError("lease expired")

    monkeypatch.setattr(agent_identity, "validate_external_identity", expired)
    with pytest.raises(ValueError, match="lease expired"):
        if operation == "observe":
            compaction.observe(42)
        elif operation == "submit":
            compaction.submit(TARGET, idempotency_key="intent")
        else:
            compaction.status(ACCEPTED)
