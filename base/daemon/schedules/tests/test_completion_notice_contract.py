"""Completion policy keeps typed ownership through the config and JSON boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from pydantic_settings import DotEnvSettingsSource

from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.daemon.schedules.completion_notices import (
    effective_completion_notice_policy,
    pending_digests,
)
from base.daemon.schedules.completion_policy import (
    CompletionNoticePolicy,
    validate_completion_notice_policy,
)
from base.host.env.config_registry import field_editor_type


def test_database_digest_reader_returns_plain_notices() -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (1, 7, "watcher:1", "done", datetime.now(UTC))
    ]
    (digest,) = pending_digests(conn, datetime.now(UTC))
    assert (digest.notices[0].source, digest.notices[0].content) == ("watcher:1", "done")


@pytest.mark.parametrize("policy", list(CompletionNoticePolicy))
def test_policy_config_env_overlay_and_editor_keep_exact_choices(
    tmp_path: Path,
    policy: CompletionNoticePolicy,
) -> None:
    field = AgentRuntimeSettings.model_fields["completion_notice_policy"]
    assert set(get_args(field.annotation)) == set(CompletionNoticePolicy)
    assert field_editor_type(field.annotation) == ("enum", ["all", "hourly"])
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
    assert schema["properties"]["AVA_COMPLETION_NOTICE_POLICY"]["enum"] == ["all", "hourly"]


@pytest.mark.parametrize("invalid", ["unknown", "failures"])
def test_invalid_policy_is_rejected(invalid: str) -> None:
    with pytest.raises(ValueError, match="completion_notice_policy must be one of"):
        validate_completion_notice_policy(invalid)
    with pytest.raises(ValueError, match="completion_notice_policy must be one of"):
        effective_completion_notice_policy({"completion_notice_policy": invalid}, "all")
    with pytest.raises(ValidationError):
        AgentRuntimeSettings.model_validate({"completion_notice_policy": invalid})


def test_runtime_and_config_share_the_policy_owner() -> None:
    from base.daemon.schedules import completion_notices

    field = AgentRuntimeSettings.model_fields["completion_notice_policy"]
    assert completion_notices.CompletionNoticePolicy is CompletionNoticePolicy
    assert completion_notices.validate_completion_notice_policy is validate_completion_notice_policy
    assert all(
        member is CompletionNoticePolicy(member.value) for member in get_args(field.annotation)
    )
