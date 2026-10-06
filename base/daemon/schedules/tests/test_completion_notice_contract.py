"""Completion domains keep typed ownership through raw restore and JSON boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from pydantic_settings import DotEnvSettingsSource

from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.daemon.schedules.completion_notices import (
    CompletionNotice,
    CompletionNoticeOutcome,
    CompletionNoticePayloadError,
    CompletionNoticePolicy,
    completion_notice_from_metadata,
    effective_completion_notice_policy,
    pending_digests,
    validate_completion_notice_policy,
)
from base.host.env.config_registry import field_editor_type
from ops.rpc_schemas.completion import CompletionNoticeIn


@pytest.mark.parametrize("outcome", list(CompletionNoticeOutcome))
def test_wire_and_metadata_restore_the_same_outcome_owner(outcome: CompletionNoticeOutcome) -> None:
    code = 0 if outcome is CompletionNoticeOutcome.EXIT else None
    payload: dict[str, object] = {"outcome": outcome.value, "exit_code": code}
    wire = CompletionNoticeIn.model_validate(payload)
    restored = completion_notice_from_metadata("watcher:1", "done", "done", payload)
    assert wire.outcome is outcome
    assert restored is not None and restored.outcome is outcome
    assert json.loads(wire.model_dump_json()) == payload
    schema = CompletionNoticeIn.model_json_schema()
    ref = schema["properties"]["outcome"]["$ref"].removeprefix("#/$defs/")
    assert set(schema["$defs"][ref]["enum"]) == {"exit", "missed"}


@pytest.mark.parametrize("invalid", [None, "unknown", "", 3, {}])
def test_unknown_outcome_is_rejected_at_every_restore_boundary(invalid: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        CompletionNotice("watcher:1", "done", invalid)
    with pytest.raises(CompletionNoticePayloadError, match="completion_notice_payload"):
        completion_notice_from_metadata("watcher:1", "done", "done", {"outcome": invalid})
    with pytest.raises(ValidationError):
        CompletionNoticeIn.model_validate({"outcome": invalid})
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (1, 7, "watcher:1", "done", invalid, None, datetime.now(UTC))
    ]
    with pytest.raises(ValueError, match="unknown completion notice outcome in database"):
        pending_digests(conn, datetime.now(UTC))


@pytest.mark.parametrize("outcome", list(CompletionNoticeOutcome))
def test_database_digest_reader_returns_enum_members(outcome: CompletionNoticeOutcome) -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (
            1,
            7,
            "watcher:1",
            "done",
            outcome.value,
            0 if outcome is CompletionNoticeOutcome.EXIT else None,
            datetime.now(UTC),
        )
    ]
    (digest,) = pending_digests(conn, datetime.now(UTC))
    assert digest.notices[0].outcome is outcome
    assert digest.notices[0].failed is (outcome is CompletionNoticeOutcome.MISSED)


@pytest.mark.parametrize("policy", list(CompletionNoticePolicy))
def test_policy_config_env_overlay_and_editor_keep_exact_choices(
    tmp_path: Path,
    policy: CompletionNoticePolicy,
) -> None:
    field = AgentRuntimeSettings.model_fields["completion_notice_policy"]
    assert set(get_args(field.annotation)) == set(CompletionNoticePolicy)
    assert field_editor_type(field.annotation) == ("enum", ["all", "failures", "hourly"])
    env_file = tmp_path / ".env"
    env_file.write_text(f"AVA_COMPLETION_NOTICE_POLICY={policy.value}\n")
    source = DotEnvSettingsSource(AgentRuntimeSettings, env_file=env_file)
    settings = AgentRuntimeSettings.model_validate(source())
    assert settings.completion_notice_policy is policy
    assert settings.model_dump(mode="json")["completion_notice_policy"] == policy.value
    assert validate_completion_notice_policy(policy.value) is policy
    assert (
        effective_completion_notice_policy({"completion_notice_policy": policy.value}, "all")
        is policy
    )
    schema = AgentRuntimeSettings.model_json_schema()
    assert schema["properties"]["AVA_COMPLETION_NOTICE_POLICY"]["enum"] == [
        "all",
        "failures",
        "hourly",
    ]


def test_invalid_policy_cannot_become_success_suppression() -> None:
    with pytest.raises(ValueError, match="completion_notice_policy must be one of"):
        effective_completion_notice_policy({"completion_notice_policy": "unknown"}, "all")
    with pytest.raises(ValidationError):
        AgentRuntimeSettings.model_validate({"completion_notice_policy": "unknown"})


def test_generated_rpc_outcome_ref_keeps_exact_wire_values() -> None:
    root = Path(__file__).resolve().parents[4]
    schemas = json.loads((root / "ui/web/openapi.json").read_text())["components"]["schemas"]
    reference = schemas["CompletionNoticeIn"]["properties"]["outcome"]["$ref"]
    owner = reference.removeprefix("#/components/schemas/")
    assert owner == "CompletionNoticeOutcome"
    assert set(schemas[owner]["enum"]) == {"exit", "missed"}
