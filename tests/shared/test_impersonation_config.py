"""Delivery policy is registered, validated and distributed as cluster config."""

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from shared.config import FIELD_INFOS, bootstrap_config_values
from shared.config.agent_runtime import AgentRuntimeSettings


def test_delivery_policy_defaults() -> None:
    configured = AgentRuntimeSettings()
    assert configured.impersonation_ack_window_seconds == 180
    assert configured.impersonation_max_delivery_attempts == 2


@pytest.mark.parametrize(
    "name", ["impersonation_ack_window_seconds", "impersonation_max_delivery_attempts"]
)
def test_delivery_policy_is_configurable_and_bootstrapped(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import config, runtime_config

    alias = f"AVA_{name.upper()}"
    info = cast(FieldInfo, FIELD_INFOS[name])
    assert info.alias == alias
    extra = info.json_schema_extra
    assert isinstance(extra, dict)
    assert extra["scope"] == "cluster-pinned"
    assert extra["writable"] is True
    assert not extra["restart_required"]
    monkeypatch.setenv(alias, "17")
    assert getattr(AgentRuntimeSettings(), name) == 17
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    (tmp_path / ".env").write_text(
        f"{alias}=17\nAVA_DB_URL=postgresql://ava@localhost:5433/ava\n"
        "AVA_RUNNER_DB_PASSWORD=test-only\n"
    )
    assert name in config.BOOTSTRAP_FIELDS
    assert bootstrap_config_values()[alias] == "17"


@pytest.mark.parametrize(
    "name", ["impersonation_ack_window_seconds", "impersonation_max_delivery_attempts"]
)
@pytest.mark.parametrize("value", [0, -1, 1.5, 2147483648, "not-a-number"])
def test_delivery_policy_rejects_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AgentRuntimeSettings.model_validate({name: value})
