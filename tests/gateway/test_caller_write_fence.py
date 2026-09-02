"""Manual external-source writers cannot outrun the consumer rollout gate."""

import pytest
from pydantic import BaseModel, ValidationError

from ops.rpc_schemas import (
    AgentMessageIn,
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
        (AgentMessageIn, "source", {"content": "hello"}),
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
