import pytest
from pydantic import ValidationError

from ops.rpc_schemas import LaunchAgentRequest, OpResponse, OpStatus


@pytest.mark.parametrize("status", list(OpStatus))
def test_op_envelope_restores_its_owner(status: OpStatus) -> None:
    response = OpResponse.model_validate({"status": status.value, "result": {"ok": True}})
    assert response.status is status
    assert response.model_dump(mode="json") == {"status": status.value, "result": {"ok": True}}


@pytest.mark.parametrize("raw", ["pending", None, ""])
def test_unknown_op_status_fails_at_wire_boundary(raw: object) -> None:
    with pytest.raises(ValidationError):
        OpResponse.model_validate({"status": raw, "result": {}})


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
