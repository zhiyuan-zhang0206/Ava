"""Agent admission and client connection budgets are independent settings."""

import pytest
from pydantic import ValidationError

from shared.config.daemon import DaemonSettings


def test_default_admission_does_not_limit_waiting_agents() -> None:
    assert DaemonSettings.model_fields["host_max_concurrent_turns"].default == 0


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
