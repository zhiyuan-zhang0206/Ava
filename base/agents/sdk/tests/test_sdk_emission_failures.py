"""SDK emission failures preserve the body outcome and explicit sampling owner."""

import asyncio
from typing import Any

import pytest

from base import telemetry
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.agents.sdk.tally import SdkCallTally


@pytest.fixture
def sampling_owner() -> call_policy.SamplingPolicyOwner:
    return call_policy.SamplingPolicyOwner()


@pytest.fixture(autouse=True)
def _full_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)


def test_simple_emit_unknown_error_is_not_recovered(
    monkeypatch: pytest.MonkeyPatch, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    failure = RuntimeError("emitter programming error")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(telemetry, "emit", broken)
    with pytest.raises(RuntimeError) as raised:
        sdk_usage_telemetry.emit("files.write", identity={}, sampling_owner=sampling_owner)
    assert raised.value is failure


@pytest.mark.parametrize("async_call", [False, True])
async def test_successful_body_exposes_unknown_emit_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    async_call: bool,
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    failure = RuntimeError("emitter programming error")
    effects: list[str] = []
    tally = SdkCallTally()

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    def body() -> str:
        effects.append("committed")
        return "done"

    async def async_body() -> str:
        return body()

    monkeypatch.setattr(telemetry, "emit", broken)
    with pytest.raises(RuntimeError) as raised:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "files.write",
                async_body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "files.write",
                body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
            )
    assert raised.value is failure
    assert effects == ["committed"]
    assert tally.snapshot() == {"files.write": 1}


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("primary_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt])
async def test_emit_secondary_failure_keeps_body_identity_cause_and_tally(
    monkeypatch: pytest.MonkeyPatch,
    async_call: bool,
    primary_type: type[BaseException],
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    primary = primary_type("body outcome")
    cause = LookupError("original cause")
    secondary = RuntimeError("emitter programming error")
    effects: list[str] = []
    tally = SdkCallTally()

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise secondary

    def body() -> None:
        effects.append("committed")
        raise primary from cause

    async def async_body() -> None:
        body()

    # Reach the cleanup seam independently of emit()'s own former blanket catch.
    monkeypatch.setattr(sdk_usage_telemetry, "emit", broken)
    with pytest.raises(primary_type) as raised:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "files.write",
                async_body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "files.write",
                body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
            )
    assert raised.value is primary
    assert primary.__cause__ is cause
    assert any(
        "RuntimeError" in note and "emitter programming error" in note for note in primary.__notes__
    )
    assert effects == ["committed"]
    assert tally.snapshot() == {"files.write": 1}
