"""The provider runner projection for agents on a remote-managed gateway plane.

A remote-managed plane has no write generation: its gateway-local launcher
projects the provider `ava_runner` from one fresh `.env` snapshot. A pure
agent-runner never projects a login; its services receive the installed unit
capability.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from base.cluster import derive

_OWNER_URL = "postgresql://ava:owner-password@127.0.0.1:5433/ava"
_RUNNER_URL = "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"


def test_projects_owner_url_to_runner_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "base.host.env.runtime_config.read_env_aliases",
        Mock(return_value={"AVA_DB_URL": _OWNER_URL, "AVA_RUNNER_DB_PASSWORD": "runner-password"}),
    )

    assert derive.runner_db_url_projection() == _RUNNER_URL


def test_missing_runner_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "base.host.env.runtime_config.read_env_aliases",
        Mock(return_value={"AVA_DB_URL": _OWNER_URL}),
    )

    with pytest.raises(RuntimeError, match="AVA_RUNNER_DB_PASSWORD is missing"):
        derive.runner_db_url_projection()


def test_gateway_url_and_password_come_from_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", Mock(return_value=True))
    snapshot = Mock(
        return_value={
            "AVA_DB_URL": "postgresql://ava:new-owner@db-new:6000/new-database",
            "AVA_RUNNER_DB_PASSWORD": "new-runner",
        }
    )
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", snapshot)
    assert derive.runner_db_url_projection() == (
        "postgresql://ava_runner:new-runner@db-new:6000/new-database"
    )
    snapshot.assert_called_once_with()


def test_missing_snapshot_url_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "base.host.env.runtime_config.read_env_aliases",
        Mock(return_value={"AVA_RUNNER_DB_PASSWORD": "new-runner"}),
    )
    with pytest.raises(RuntimeError, match="AVA_DB_URL is missing"):
        derive.runner_db_url_projection()


def test_pure_runner_never_projects_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure agent-runner has no provider credential and no bootstrap-served
    login to pass through: the projection refuses without reading anything."""
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", Mock(return_value=False))
    snapshot = Mock(side_effect=AssertionError("must not read local credentials"))
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", snapshot)
    with pytest.raises(RuntimeError, match="installed unit capability"):
        derive.runner_db_url_projection()
    snapshot.assert_not_called()


def test_malformed_config_warning_does_not_log_raw_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config.service_read import _warn_undecodable_field

    warning = Mock()
    monkeypatch.setattr("base.log.logger.warning", warning)
    _warn_undecodable_field("db_url", "AVA_DB_URL", _OWNER_URL)
    warning.assert_called_once()
    assert "AVA_DB_URL" in warning.call_args.args[0]
    assert "owner-password" not in warning.call_args.args[0]
    assert _OWNER_URL not in warning.call_args.args[0]
