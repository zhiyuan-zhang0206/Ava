"""Runner database URL projection for every agent-profile launch path."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from shared.cluster import derive

_OWNER_URL = "postgresql://ava:owner-password@127.0.0.1:5433/ava"
_RUNNER_URL = "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"


def test_projects_owner_url_to_runner_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "shared.runtime_config.read_env_aliases",
        Mock(return_value={"AVA_DB_URL": _OWNER_URL, "AVA_RUNNER_DB_PASSWORD": "runner-password"}),
    )

    assert derive.runner_db_url_projection(_OWNER_URL) == _RUNNER_URL


def test_runner_url_passes_through_without_reading_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_password_read() -> str:
        pytest.fail("runner projection must not replace an already-runner URL")

    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=False))
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", _unexpected_password_read)

    assert derive.runner_db_url_projection(_RUNNER_URL) == _RUNNER_URL


def test_missing_runner_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "shared.runtime_config.read_env_aliases", Mock(return_value={"AVA_DB_URL": _OWNER_URL})
    )

    with pytest.raises(RuntimeError, match="AVA_RUNNER_DB_PASSWORD is not set"):
        derive.runner_db_url_projection(_OWNER_URL)


@pytest.mark.parametrize("cached_url", [_OWNER_URL, _RUNNER_URL])
def test_gateway_url_and_password_come_from_one_snapshot(
    monkeypatch: pytest.MonkeyPatch, cached_url: str
) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=True))
    snapshot = Mock(
        return_value={
            "AVA_DB_URL": "postgresql://ava:new-owner@db-new:6000/new-database",
            "AVA_RUNNER_DB_PASSWORD": "new-runner",
        }
    )
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", snapshot)
    assert derive.runner_db_url_projection(cached_url) == (
        "postgresql://ava_runner:new-runner@db-new:6000/new-database"
    )
    snapshot.assert_called_once_with()


def test_missing_snapshot_url_cannot_fall_back_to_cached_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        "shared.runtime_config.read_env_aliases",
        Mock(
            return_value={
                "AVA_RUNNER_DB_PASSWORD": "new-runner",
            }
        ),
    )
    with pytest.raises(RuntimeError, match="AVA_DB_URL is missing"):
        derive.runner_db_url_projection(_OWNER_URL)


def test_remote_owner_projection_never_reads_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", Mock(return_value=False))
    snapshot = Mock(side_effect=AssertionError("must not read local credentials"))
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", snapshot)
    with pytest.raises(RuntimeError, match="authenticated bootstrap"):
        derive.runner_db_url_projection(_OWNER_URL)
    snapshot.assert_not_called()


def test_malformed_config_warning_does_not_log_raw_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config.service_read import _warn_undecodable_field

    warning = Mock()
    monkeypatch.setattr("shared.log.logger.warning", warning)
    _warn_undecodable_field("db_url", "AVA_DB_URL", _OWNER_URL)
    warning.assert_called_once()
    assert "AVA_DB_URL" in warning.call_args.args[0]
    assert "owner-password" not in warning.call_args.args[0]
    assert _OWNER_URL not in warning.call_args.args[0]
