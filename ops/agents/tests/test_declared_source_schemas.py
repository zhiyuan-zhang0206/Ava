import pytest
from pydantic import BaseModel, ValidationError

from ops.rpc_schemas import (
    LaunchAgentRequest,
    RestartAgentRequest,
    ResurrectAgentRequest,
    SpawnAgentRequest,
    TerminateAgentRequest,
)

_SCHEMAS: list[tuple[type[BaseModel], str, dict[str, object]]] = [
    (SpawnAgentRequest, "prompt_source", {"prompt": "hello"}),
    (ResurrectAgentRequest, "resurrected_by", {}),
    (RestartAgentRequest, "source", {}),
    (TerminateAgentRequest, "source", {"force": True}),
]


@pytest.mark.parametrize(
    "source", ["external_agent:codex:run-42", "external_agent:claude_code", "user"]
)
@pytest.mark.parametrize(("schema", "field", "other"), _SCHEMAS)
def test_declared_source_is_accepted(
    schema: type[BaseModel], field: str, other: dict[str, object], source: str
) -> None:
    assert schema.model_validate(other | {field: source}).model_dump()[field] == source


@pytest.mark.parametrize(
    "source",
    ["external_agent:", "external_agent:a:b:c", "external_agent:bad name", "unknown:legacy"],
)
@pytest.mark.parametrize(("schema", "field", "other"), _SCHEMAS)
def test_malformed_declaration_is_rejected_before_dispatch(
    schema: type[BaseModel], field: str, other: dict[str, object], source: str
) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(other | {field: source})


@pytest.mark.parametrize("schema", [TerminateAgentRequest, RestartAgentRequest])
@pytest.mark.parametrize("reason", ["machine-pause", "incident-operator", "self"])
def test_legacy_lifecycle_audit_reason_is_not_chat_envelope_grammar(
    schema: type[BaseModel], reason: str
) -> None:
    assert schema.model_validate({"source": reason}).model_dump()["source"] == reason


@pytest.mark.parametrize("field", ["prompt", "prompt_source", "label"])
def test_launch_rejects_retired_gateway_birth_fields(field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        LaunchAgentRequest.model_validate(
            {
                "agent_id": 42,
                "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                field: "hello",
            }
        )
    assert [(error["loc"], error["type"]) for error in refused.value.errors()] == [
        ((field,), "extra_forbidden")
    ]
