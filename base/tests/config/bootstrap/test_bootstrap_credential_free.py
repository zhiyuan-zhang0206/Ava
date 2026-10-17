from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from base import config
from base.config.service_read import ConfigAuthority
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_RUNNER_PW = "runner-secret-token"
_DB_URL = "postgresql://meiva@127.0.0.1:5433/meiva"


def _pg_url(pw: str, *, host: str) -> str:
    return f"postgresql://meiva:{pw}@{host}/meiva"


def _write_gateway_env(tmp_path: Path, runner_pw: str | None = None, db_url: str = _DB_URL) -> None:
    lines = [
        f"MEIVA_DB_URL={db_url}",
        "MEIVA_REDIS_ADMIN_PASSWORD=redis-admin-only",
        "MEIVA_REDIS_PASSWORD=redis-runtime-only",
    ]
    if runner_pw is not None:
        lines.append(f"MEIVA_RUNNER_DB_PASSWORD={runner_pw}")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")


def _served(tmp_path: Path) -> dict[str, str]:
    authority = ConfigAuthority(config.settings, config.settings, tmp_path / ".env")
    return config.bootstrap_config_values(authority, provider_key_envs=(), plugin_cluster_config="")


def test_local_plane_serves_the_endpoint_not_its_active_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    secret = seed_write_generation(tmp_path)
    vals = _served(tmp_path)
    parts = urlsplit(vals["MEIVA_DB_URL"])
    assert (parts.username, parts.password) == ("meiva", None)
    assert (parts.hostname, parts.port, parts.path) == ("127.0.0.1", 5433, "/meiva")
    payload = "".join(vals.values())
    for role in (secret.roles.gateway, secret.roles.runner):
        assert role.name not in payload
        assert role.password not in payload
    assert _RUNNER_PW not in payload
    assert "MEIVA_RUNNER_DB_PASSWORD" not in vals
    assert "MEIVA_REDIS_ADMIN_PASSWORD" not in vals
    assert "MEIVA_REDIS_PASSWORD" not in vals


def test_home_without_a_ledger_serves_the_endpoint_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    assert urlsplit(_served(tmp_path)["MEIVA_DB_URL"]).password is None


def test_remote_plane_strips_the_provider_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider_url = _pg_url("provider-pw", host="db.provider.example:5432")
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW, db_url=provider_url)
    monkeypatch.setattr(config.settings.data_plane, "db_url", provider_url)
    vals = _served(tmp_path)
    parts = urlsplit(vals["MEIVA_DB_URL"])
    assert (parts.username, parts.password, parts.hostname) == (
        "meiva",
        None,
        "db.provider.example",
    )
    assert "provider-pw" not in "".join(vals.values())
    assert _RUNNER_PW not in "".join(vals.values())


@pytest.mark.parametrize("missing_url", [None, ""])
def test_missing_url_refuses_before_boot_time_fallback(
    monkeypatch: pytest.MonkeyPatch,
    missing_url: str | None,
    tmp_path: Path,
    loguru_records: LogRecords,
) -> None:
    snapshot = {"MEIVA_RUNNER_DB_PASSWORD": "new-private-password"}
    if missing_url is not None:
        snapshot["MEIVA_DB_URL"] = missing_url
    read = Mock(return_value=snapshot)

    def read_snapshot(_owner: ConfigAuthority) -> dict[str, str]:
        return read()

    monkeypatch.setattr(ConfigAuthority, "read_env_aliases", read_snapshot)
    with pytest.raises(ValueError, match="MEIVA_DB_URL is missing") as caught:
        config.bootstrap_config_values(
            ConfigAuthority(config.settings, config.settings, tmp_path / ".env"),
            provider_key_envs=(),
            plugin_cluster_config="",
        )
    read.assert_called_once_with()
    assert loguru_records.messages("WARNING") == []
    assert "new-private-password" not in str(caught.value)


def test_served_payload_excludes_the_offsite_backup_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text(
        env_path.read_text()
        + "MEIVA_BACKUP_OFFSITE_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com\n"
        + "MEIVA_BACKUP_OFFSITE_BUCKET=backups\n"
        + "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE=/private/oss.json\n"
        + "MEIVA_WALG_CONFIG_FILE=/private/walg.json\n"
    )
    vals = _served(tmp_path)
    for alias in (
        "MEIVA_BACKUP_OFFSITE_ENDPOINT",
        "MEIVA_BACKUP_OFFSITE_BUCKET",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        "MEIVA_WALG_CONFIG_FILE",
    ):
        assert alias not in vals
