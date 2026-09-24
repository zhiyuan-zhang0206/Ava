"""Agent admission and client connection budgets are independent settings."""

import pytest
from pydantic import ValidationError

from shared.config.daemon import DaemonSettings


def test_default_admission_does_not_limit_waiting_agents() -> None:
    assert DaemonSettings.model_fields["host_max_concurrent_turns"].default == 0


def test_recovery_wake_batch_default_bounds_and_metadata() -> None:
    field = DaemonSettings.model_fields["host_recovery_wake_batch"]
    assert field.default == 4
    assert DaemonSettings(AVA_HOST_RECOVERY_WAKE_BATCH=1).host_recovery_wake_batch == 1
    with pytest.raises(ValidationError):
        DaemonSettings(AVA_HOST_RECOVERY_WAKE_BATCH=0)
    assert field.json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "agent",
        "writable": True,
        "sensitive": False,
        "scope": "cluster-pinned",
    }


def test_recovery_wake_inflight_default_bounds_and_metadata() -> None:
    field = DaemonSettings.model_fields["host_recovery_wake_inflight"]
    assert field.default == 4
    assert DaemonSettings(AVA_HOST_RECOVERY_WAKE_INFLIGHT=1).host_recovery_wake_inflight == 1
    with pytest.raises(ValidationError):
        DaemonSettings(AVA_HOST_RECOVERY_WAKE_INFLIGHT=0)
    assert field.json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "agent",
        "writable": True,
        "sensitive": False,
        "scope": "cluster-pinned",
    }


@pytest.mark.parametrize(
    ("name", "default", "valid", "invalid"),
    [
        ("host_db_recovery_prolonged_attempts", 6, 1, 0),
        ("host_db_recovery_prolonged_seconds", 300.0, 0.1, 0.0),
        ("host_db_recovery_budget_seconds", 3600.0, 600.0, 599.9),
    ],
)
def test_db_recovery_defaults_bounds_and_metadata(
    name: str, default: float, valid: float, invalid: float
) -> None:
    field = DaemonSettings.model_fields[name]
    alias = f"AVA_{name.upper()}"
    assert field.default == default
    assert field.alias == alias
    assert getattr(DaemonSettings.model_validate({alias: valid}), name) == valid
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({alias: invalid})
    assert field.json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "agent",
        "writable": True,
        "sensitive": False,
        "scope": "cluster-pinned",
    }


@pytest.mark.parametrize("prolonged_seconds", [600.0, 601.0])
def test_db_recovery_prolonged_warning_must_precede_budget(prolonged_seconds: float) -> None:
    with pytest.raises(
        ValidationError,
        match="host_db_recovery_prolonged_seconds must be below host_db_recovery_budget_seconds",
    ):
        DaemonSettings(
            AVA_HOST_DB_RECOVERY_PROLONGED_SECONDS=prolonged_seconds,
            AVA_HOST_DB_RECOVERY_BUDGET_SECONDS=600.0,
        )


@pytest.mark.parametrize("limit", [0, 1, 1000])
def test_admission_does_not_resize_database_pools(limit: int) -> None:
    config = DaemonSettings(
        AVA_HOST_MAX_CONCURRENT_TURNS=limit,
        AVA_HOST_DB_POOL_MAX_SIZE=64,
        AVA_HOST_CONTROL_POOL_MAX_SIZE=8,
    )
    assert config.host_max_concurrent_turns == limit
    assert config.host_db_pool_max_size == 64
    assert config.host_control_pool_max_size == 8


@pytest.mark.parametrize(
    ("alias", "value"),
    [
        ("AVA_HOST_MAX_CONCURRENT_TURNS", -1),
        ("AVA_HOST_DB_POOL_MAX_SIZE", 0),
        ("AVA_HOST_DB_POOL_MAX_SIZE", -1),
        ("AVA_HOST_CONTROL_POOL_MAX_SIZE", 0),
        ("AVA_HOST_CONTROL_POOL_MAX_SIZE", -1),
    ],
)
def test_invalid_capacity_fails_at_configuration_load(alias: str, value: int) -> None:
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({alias: value})


@pytest.mark.parametrize(
    "field", ["host_max_concurrent_turns", "host_db_pool_max_size", "host_control_pool_max_size"]
)
def test_capacity_is_cluster_pinned_and_requires_host_restart(field: str) -> None:
    metadata = DaemonSettings.model_fields[field].json_schema_extra
    assert isinstance(metadata, dict)
    assert metadata["capability"] == "agent-runner"
    assert metadata["scope"] == "cluster-pinned"
    assert metadata["restart_required"] == "all"
