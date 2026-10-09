"""The installed SDK keeps a terminal send receipt through emission cleanup."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import ava
from ava.gateway_client.transport import use_client
from ava.sdk_surface import metering
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage
from base.agents.sdk.tally import SdkCallTally
from tests.fixtures.pin_agent import pin_agent


@pytest.mark.parametrize("committed", [False, True])
def test_installed_send_keeps_unknown_response_when_emission_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    pin_agent(7, owns_loop=False)
    tally = SdkCallTally()
    ava.bind_context(replace(ava.context, sdk_calls=tally))
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
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
        ledger = metering.install()
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                ava.agents.send_message(42, "one logical send")
        finally:
            metering.uninstall(ledger)
    assert len(responses) == 1
    assert raised.value.response is responses[0]
    assert raised.value.__cause__ is None
    assert raised.value.response.json()["receipt"]["client_message_id"]
    assert any("SDK emitter code failed" in note for note in raised.value.__notes__)
    assert tally.snapshot() == {"agents.send_message": 1}
    assert not list((tmp_path / "state" / "delivery-outbox").glob("*.json"))
