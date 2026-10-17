from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from base.cluster import derive
from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.host.env import bootstrap, dotenv_file
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_OWNER_URL = "postgresql://meiva:owner-password@127.0.0.1:5433/meiva"
_RUNNER_URL = "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"


def test_projects_owner_url_to_runner_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        dotenv_file,
        "read_env_aliases",
        Mock(
            return_value={"MEIVA_DB_URL": _OWNER_URL, "MEIVA_RUNNER_DB_PASSWORD": "runner-password"}
        ),
    )

    assert derive.runner_db_url_projection() == _RUNNER_URL


def test_missing_runner_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        dotenv_file,
        "read_env_aliases",
        Mock(return_value={"MEIVA_DB_URL": _OWNER_URL}),
    )

    with pytest.raises(RuntimeError, match="MEIVA_RUNNER_DB_PASSWORD is missing"):
        derive.runner_db_url_projection()


def test_gateway_url_and_password_come_from_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", Mock(return_value=True))
    snapshot = Mock(
        return_value={
            "MEIVA_DB_URL": "postgresql://meiva:new-owner@db-new:6000/new-database",
            "MEIVA_RUNNER_DB_PASSWORD": "new-runner",
        }
    )
    monkeypatch.setattr(dotenv_file, "read_env_aliases", snapshot)
    assert derive.runner_db_url_projection() == (
        "postgresql://meiva_runner:new-runner@db-new:6000/new-database"
    )
    snapshot.assert_called_once_with()


def test_missing_snapshot_url_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", Mock(return_value=True))
    monkeypatch.setattr(
        dotenv_file,
        "read_env_aliases",
        Mock(return_value={"MEIVA_RUNNER_DB_PASSWORD": "new-runner"}),
    )
    with pytest.raises(RuntimeError, match="MEIVA_DB_URL is missing"):
        derive.runner_db_url_projection()


def test_pure_runner_never_projects_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", Mock(return_value=False))
    snapshot = Mock(side_effect=AssertionError("must not read local credentials"))
    monkeypatch.setattr(dotenv_file, "read_env_aliases", snapshot)
    with pytest.raises(RuntimeError, match="installed unit capability"):
        derive.runner_db_url_projection()
    snapshot.assert_not_called()


def test_malformed_config_read_does_not_log_raw_credentials(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    runtime = Settings(profile=None)
    authority = ConfigAuthority(runtime, runtime, tmp_path / ".env")
    authority.env_path.write_text("MEIVA_DB_URL=postgresql://owner-password@[::1\n")
    loguru_records.clear()

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert loguru_records.messages("WARNING") == []
    assert loguru_records.messages("DEBUG") == []
