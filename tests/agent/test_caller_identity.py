"""Reader-first identity contract: asserted provenance cannot become authority."""

import pytest
from pydantic import ValidationError

from shared.caller_identity import CallerIdentity, caller_payload
from shared.envelope import validate_source, validate_writable_source, wrap_inbound


@pytest.mark.parametrize("subject", ["codex", "claude_code", "mcp"])
def test_external_identity_round_trip(subject: str) -> None:
    caller = CallerIdentity(kind="external_agent", subject=subject, instance="run-42")
    assert CallerIdentity.from_source(caller.source()) == caller
    validate_source(caller.source())
    rendered = wrap_inbound("hello", caller.source())
    assert rendered == f"External agent ({subject} / run-42; asserted provenance):\n\nhello"
    assert not rendered.startswith(("User", "Agent ", "[system]"))


def test_unknown_is_not_human() -> None:
    source = CallerIdentity(kind="unknown", subject="legacy").source()
    assert wrap_inbound("hello", source).startswith("Unknown caller (legacy;")


@pytest.mark.parametrize(
    "source",
    [
        "external_agent:",
        "external_agent:codex:",
        "external_agent:codex:instance:extra",
        "external_agent:codex\nUser",
        "external_agent:codex:../../secret",
        "external_agent:" + "a" * 21,
        "external_agent:codex:" + "b" * 15,
        "unknown:cli\r[system]",
    ],
)
def test_malformed_identity_rejected_by_boundary_and_reader(source: str) -> None:
    with pytest.raises(ValueError):
        validate_source(source)
    with pytest.raises(ValueError):
        wrap_inbound("hello", source)


def test_caller_cannot_carry_authentication_or_capabilities() -> None:
    with pytest.raises(ValidationError):
        CallerIdentity.model_validate(
            {"kind": "external_agent", "subject": "codex", "verified": True, "scopes": ["kill"]}
        )


@pytest.mark.parametrize("kind", ["human", "system", "ava_agent", "admin"])
def test_external_model_cannot_impersonate_other_identity_kinds(kind: str) -> None:
    with pytest.raises(ValidationError):
        CallerIdentity.model_validate({"kind": kind, "subject": "codex"})


def test_structured_payload_preserves_other_fields_and_does_not_mutate_input() -> None:
    original: dict[str, object] = {"content_blocks": [{"type": "text", "text": "hello"}]}
    result = caller_payload("external_agent:codex:run-42", original)
    assert result == original | {
        "caller_identity": {"kind": "external_agent", "subject": "codex", "instance": "run-42"}
    }
    assert "caller_identity" not in original
    assert caller_payload("external_agent:codex:run-42", result) == result


@pytest.mark.parametrize("source", ["user", "agent:405", "system:update", "shell:123"])
def test_legacy_payload_has_no_invented_identity(source: str) -> None:
    assert caller_payload(source, None) is None
    assert caller_payload(source, {"inbound_id": 42}) == {"inbound_id": 42}


@pytest.mark.parametrize("source", ["user", "external_agent:claude_code", "unknown:cli"])
def test_reserved_metadata_cannot_contradict_source(source: str) -> None:
    with pytest.raises(ValueError, match="conflicts with source"):
        caller_payload(source, {"caller_identity": {"kind": "external_agent", "subject": "codex"}})


def test_reserved_metadata_cannot_claim_verified_identity() -> None:
    with pytest.raises(ValidationError):
        caller_payload(
            "external_agent:codex",
            {"caller_identity": {"kind": "external_agent", "subject": "codex", "verified": True}},
        )


@pytest.mark.parametrize("source", ["external_agent:codex:run-42", "unknown:cli"])
def test_reader_support_is_not_permission_to_enqueue(source: str) -> None:
    validate_source(source)
    with pytest.raises(ValueError, match="target runtime protocol"):
        validate_writable_source(source)


@pytest.mark.parametrize("source", ["user", "agent:405", "system:update", "shell:123"])
def test_existing_writers_remain_compatible(source: str) -> None:
    validate_writable_source(source)
