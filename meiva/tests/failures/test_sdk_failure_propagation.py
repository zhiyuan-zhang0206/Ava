from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import meiva
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage
from base.agents.sdk.tally import SdkCallTally
from meiva.gateway_client import use_client
from meiva.sdk_surface import metering, process_context
from meiva.sdk_surface.installation import Installation
from meiva.tests.fixtures.pin_agent import pin_agent


@pytest.mark.parametrize("committed", [False, True])
def test_installed_send_keeps_unknown_response_when_emission_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
    model_installation: Installation,
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    pin_agent(7, owns_loop=False)
    tally = SdkCallTally()
    process_context.bind_context(replace(meiva.context, sdk_calls=tally))
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    responses: list[httpx.Response] = []
    emission_failure = RuntimeError("SDK emitter code failed")

    def reply(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            500,
            request=request,
            json={
                "reason": "unknown_postcommit_failure",
                "detail": "message consumer failed",
                "committed": committed,
                "retryable": False,
                "receipt": {"client_message_id": request.headers["Idempotency-Key"]},
            },
        )
        responses.append(response)
        return response

    def fail_emit(*_args: Any, **_kwargs: Any) -> None:
        raise emission_failure

    monkeypatch.setattr(sdk_usage, "emit", fail_emit)
    with (
        httpx.Client(
            base_url="http://gateway.test", transport=httpx.MockTransport(reply)
        ) as client,
        use_client(client),
    ):
        ledger = metering.install(call_policy.SamplingPolicyOwner())
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                meiva.agents.send_message(42, "one logical send")
        finally:
            metering.uninstall(ledger)
    assert len(responses) == 1
    assert raised.value.response is responses[0]
    assert raised.value.__cause__ is None
    assert raised.value.response.json()["receipt"]["client_message_id"]
    assert any("SDK emitter code failed" in note for note in raised.value.__notes__)
    assert tally.snapshot() == {"agents.send_message": 1}
    assert not list((tmp_path / "state" / "delivery-outbox").glob("*.json"))
