"""`GET /api/bootstrap` serves configuration, never a database login.

The served `AVA_DB_URL` is the credential-free endpoint whatever the gateway
home holds: a local plane's active write generation is not projected, a
remote-managed plane's provider password is stripped, and the retired runner
password key is never served. A runner's login arrives only through its
installed unit capability (`base.cluster.authority.unit`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from base import config
from base.config.service_read import ConfigAuthority

_RUNNER_PW = "runner-secret-token"
_DB_URL = "postgresql://ava@127.0.0.1:5433/ava"


def _pg_url(pw: str, *, host: str) -> str:
    """A credentialed postgres URL built from parts, so the source carries no
    `scheme://user:password@host` literal for a secret scanner to flag (same
    convention as base/tests/test_url_secret.py)."""
    return f"postgresql://ava:{pw}@{host}/ava"


def _write_gateway_env(tmp_path: Path, runner_pw: str | None = None, db_url: str = _DB_URL) -> None:
    lines = [
        f"AVA_DB_URL={db_url}",
        "AVA_REDIS_ADMIN_PASSWORD=redis-admin-only",
        "AVA_REDIS_PASSWORD=redis-runtime-only",
    ]
    if runner_pw is not None:
        lines.append(f"AVA_RUNNER_DB_PASSWORD={runner_pw}")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")


def _served(tmp_path: Path) -> dict[str, str]:
    """Read the gateway payload through the authority owning this test file."""
    authority = ConfigAuthority(config.settings, config.settings, tmp_path / ".env")
    return config.bootstrap_config_values(authority, provider_key_envs=(), plugin_cluster_config="")


def test_local_plane_serves_the_endpoint_not_its_active_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    """A stale runner holding the bearer can no longer reacquire the current
    generation here: the ledger's logins never enter the payload."""
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    secret = seed_write_generation(tmp_path)
    vals = _served(tmp_path)
    parts = urlsplit(vals["AVA_DB_URL"])
    assert (parts.username, parts.password) == ("ava", None)
    assert (parts.hostname, parts.port, parts.path) == ("127.0.0.1", 5433, "/ava")
    payload = "".join(vals.values())
    for role in (secret.roles.gateway, secret.roles.runner):
        assert role.name not in payload
        assert role.password not in payload
    assert _RUNNER_PW not in payload
    assert "AVA_RUNNER_DB_PASSWORD" not in vals
    assert "AVA_REDIS_ADMIN_PASSWORD" not in vals
    assert "AVA_REDIS_PASSWORD" not in vals


def test_home_without_a_ledger_serves_the_endpoint_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Serving no login needs no authority: the payload no longer depends on
    the ledger, so a home without one serves the same credential-free shape."""
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    assert urlsplit(_served(tmp_path)["AVA_DB_URL"]).password is None


def test_remote_plane_strips_the_provider_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider_url = _pg_url("provider-pw", host="db.provider.example:5432")
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW, db_url=provider_url)
    monkeypatch.setattr(config.settings.data_plane, "db_url", provider_url)
    vals = _served(tmp_path)
    parts = urlsplit(vals["AVA_DB_URL"])
    assert (parts.username, parts.password, parts.hostname) == (
        "ava",
        None,
        "db.provider.example",
    )
    assert "provider-pw" not in "".join(vals.values())
    assert _RUNNER_PW not in "".join(vals.values())


@pytest.mark.parametrize("missing_url", [None, ""])
def test_missing_url_refuses_before_boot_time_fallback(
    monkeypatch: pytest.MonkeyPatch, missing_url: str | None, tmp_path: Path
) -> None:
    snapshot = {"AVA_RUNNER_DB_PASSWORD": "new-private-password"}
    if missing_url is not None:
        snapshot["AVA_DB_URL"] = missing_url
    read = Mock(return_value=snapshot)
    warning = Mock()

    def read_snapshot(_owner: ConfigAuthority) -> dict[str, str]:
        return read()

    monkeypatch.setattr(ConfigAuthority, "read_env_aliases", read_snapshot)
    monkeypatch.setattr("base.log.logger.warning", warning)
    with pytest.raises(ValueError, match="AVA_DB_URL is missing") as caught:
        config.bootstrap_config_values(
            ConfigAuthority(config.settings, config.settings, tmp_path / ".env"),
            provider_key_envs=(),
            plugin_cluster_config="",
        )
    read.assert_called_once_with()
    warning.assert_not_called()
    assert "new-private-password" not in str(caught.value)


def test_served_payload_excludes_the_offsite_backup_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The off-site backup destination is a gateway-local fact: the gateway alone
    runs the dump and holds the credentials file; the WAL-G config path likewise
    names a file on the gateway. Whatever the gateway's .env says, neither may ride
    the bootstrap payload to a runner."""
    _write_gateway_env(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text(
        env_path.read_text()
        + "AVA_BACKUP_OFFSITE_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com\n"
        + "AVA_BACKUP_OFFSITE_BUCKET=backups\n"
        + "AVA_BACKUP_OFFSITE_CREDENTIALS_FILE=/private/oss.json\n"
        + "AVA_WALG_CONFIG_FILE=/private/walg.json\n"
    )
    vals = _served(tmp_path)
    for alias in (
        "AVA_BACKUP_OFFSITE_ENDPOINT",
        "AVA_BACKUP_OFFSITE_BUCKET",
        "AVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        "AVA_WALG_CONFIG_FILE",
    ):
        assert alias not in vals
