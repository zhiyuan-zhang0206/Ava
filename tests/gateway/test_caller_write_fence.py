"""Manual external-source writers cannot outrun the consumer rollout gate."""

import pytest
from pydantic import BaseModel, ValidationError

from ops.rpc_schemas import (
    LaunchAgentRequest,
    RestartAgentRequest,
    ResurrectAgentRequest,
    SpawnAgentRequest,
    TerminateAgentRequest,
)


@pytest.mark.parametrize("source", ["external_agent:codex:run-42", "unknown:cli"])
@pytest.mark.parametrize(
    ("schema", "field", "other"),
    [
        (SpawnAgentRequest, "prompt_source", {"prompt": "hello"}),
        (LaunchAgentRequest, "prompt_source", {"agent_id": 42, "prompt": "hello"}),
        (ResurrectAgentRequest, "resurrected_by", {}),
        (RestartAgentRequest, "source", {}),
        (TerminateAgentRequest, "source", {"force": True}),
    ],
)
def test_manual_new_source_rejected_before_dispatch(
    schema: type[BaseModel],
    field: str,
    other: dict[str, object],
    source: str,
) -> None:
    with pytest.raises(ValidationError, match="target runtime protocol"):
        schema.model_validate(other | {field: source})


def test_direct_resurrection_rejects_before_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from ops.agent_wake import _prepare_resurrect_attempt

    transaction = Mock(side_effect=AssertionError("must not reach database"))
    monkeypatch.setattr("ops.agent_wake.write_transaction", transaction)
    with pytest.raises(ValueError, match="target runtime protocol"):
        _prepare_resurrect_attempt(
            42,
            resurrected_by="external_agent:codex",
            prompt="hello",
            trigger_inbound_id=None,
            trigger_inbound_kind=None,
        )
    transaction.assert_not_called()


@pytest.mark.parametrize("schema", [TerminateAgentRequest, RestartAgentRequest])
@pytest.mark.parametrize("reason", ["machine-pause", "incident-operator", "self"])
def test_legacy_lifecycle_audit_reason_is_not_chat_envelope_grammar(
    schema: type[BaseModel], reason: str
) -> None:
    assert schema.model_validate({"source": reason}).model_dump()["source"] == reason
