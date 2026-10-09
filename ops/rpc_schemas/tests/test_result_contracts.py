"""Separate RPC result domains have one owner each and keep their JSON values."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from base.agents import CancelResult, TerminateResult
from ops.rpc_schemas import CancelRequested, LaunchAgentRequest, OpResponse, OpStatus
from ops.rpc_schemas.billing_recovery import (
    BillingRecoveryMode,
    BillingRecoveryRunOutcome,
    BillingResurrectResponse,
)


@pytest.mark.parametrize("result", list(CancelResult))
def test_cancel_is_its_own_shared_domain(result: CancelResult) -> None:
    assert CancelResult is not TerminateResult
    assert CancelRequested.model_fields["status"].annotation is CancelResult
    response = CancelRequested.model_validate({"status": result.value})
    assert response.status is result
    assert response.model_dump(mode="json") == {"status": result.value, "inbound_id": None}


@pytest.mark.parametrize("status", list(OpStatus))
def test_op_envelope_restores_its_owner(status: OpStatus) -> None:
    response = OpResponse.model_validate({"status": status.value, "result": {"ok": True}})
    assert response.status is status
    assert response.model_dump(mode="json") == {"status": status.value, "result": {"ok": True}}


@pytest.mark.parametrize("mode", list(BillingRecoveryMode))
@pytest.mark.parametrize("outcome", list(BillingRecoveryRunOutcome))
def test_billing_run_domains_preserve_independent_wire_sets(
    mode: BillingRecoveryMode,
    outcome: BillingRecoveryRunOutcome,
) -> None:
    wire: dict[str, object] = {
        "mode": mode.value,
        "outcome": outcome.value,
        "balance": {"ok": True, "detail": "available", "threshold": 10},
        "agents": [],
        "halted_alive": [],
    }
    response = BillingResurrectResponse.model_validate(wire)
    assert response.mode is mode
    assert response.outcome is outcome
    assert response.model_dump(mode="json")["mode"] == mode.value
    assert response.model_dump(mode="json")["outcome"] == outcome.value
    schema = BillingResurrectResponse.model_json_schema()
    for field, expected in [
        ("mode", ["dry_run", "execute"]),
        ("outcome", ["preview", "executed", "refused"]),
    ]:
        ref = schema["properties"][field]["$ref"]
        assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == expected


@pytest.mark.parametrize("raw", ["pending", None, ""])
def test_unknown_cancel_and_op_status_fail_at_wire_boundary(raw: object) -> None:
    with pytest.raises(ValidationError):
        CancelRequested.model_validate({"status": raw})
    with pytest.raises(ValidationError):
        OpResponse.model_validate({"status": raw, "result": {}})


def test_generated_http_schema_resolves_the_same_exact_wire_domains() -> None:
    schemas = json.loads((Path(__file__).resolve().parents[3] / "ui/web/openapi.json").read_text())[
        "components"
    ]["schemas"]
    for model, field, owner in [
        ("CancelRequested", "status", CancelResult),
        ("BillingResurrectResponse", "mode", BillingRecoveryMode),
        ("BillingResurrectResponse", "outcome", BillingRecoveryRunOutcome),
    ]:
        ref = schemas[model]["properties"][field]["$ref"]
        assert schemas[ref.rsplit("/", 1)[1]]["enum"] == [member.value for member in owner]


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_id": 7},
        {"agent_id": 7, "launch_attempt_id": None},
        {"agent_id": 7, "launch_attempt_id": "invalid"},
        *[
            {
                "agent_id": 7,
                "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                field: None,
            }
            for field in ("prompt", "prompt_source", "label")
        ],
    ],
)
def test_launch_rejects_missing_attempt_and_retired_prompt_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        LaunchAgentRequest.model_validate(payload)
