"""Tests for the bootstrap runner projection (Task #1236, write generations).

`GET /api/bootstrap` serves the cluster's AVA_DB_URL with a runner-class login
carried INSIDE the URL, never as a standalone key. A local plane serves its
active write generation's runner login from the home's private ledger; a
remote-managed plane serves the provider's `ava_runner` with the recorded
provider credential. Admin credentials never leave the gateway, a home with
neither authority fails loudly, and an unknown role value is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from shared import config
from shared import runtime_config as rt

_RUNNER_PW = "runner-secret-token"
_DB_URL = "postgresql://ava@127.0.0.1:5433/ava"


def _pg_url(pw: str, *, host: str) -> str:
    """A credentialed postgres URL built from parts, so the source carries no
    `scheme://user:password@host` literal for a secret scanner to flag (same
    convention as tests/shared/test_url_secret.py)."""
    return f"postgresql://ava:{pw}@{host}/ava"


def _write_gateway_env(tmp_path: Path, runner_pw: str | None = None) -> None:
    lines = [
        f"AVA_DB_URL={_DB_URL}",
        "AVA_REDIS_ADMIN_PASSWORD=redis-admin-only",
        "AVA_REDIS_PASSWORD=redis-runtime-only",
    ]
    if runner_pw is not None:
        lines.append(f"AVA_RUNNER_DB_PASSWORD={runner_pw}")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")


def _projected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """bootstrap_config_values with the gateway .env pinned to tmp_path."""
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    return config.bootstrap_config_values(role="runner")


@pytest.fixture
def local_gateway(tmp_path: Path, seed_write_generation: Callable[[Path], Any]) -> Any:
    """A local-plane gateway home with an active generation; returns its secret."""
    _write_gateway_env(tmp_path)
    return seed_write_generation(tmp_path)


@pytest.mark.parametrize("role", [None, "runner"])
def test_local_plane_projects_the_active_runner_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_gateway: Any, role: str | None
) -> None:
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    vals = config.bootstrap_config_values(role=role)
    parts = urlsplit(vals["AVA_DB_URL"])
    runner = local_gateway.roles.runner
    assert (parts.username, parts.password) == (runner.name, runner.password)
    # host / port / database survive verbatim — only the identity swaps.
    assert (parts.hostname, parts.port, parts.path) == ("127.0.0.1", 5433, "/ava")
    assert "AVA_REDIS_ADMIN_PASSWORD" not in vals
    assert "AVA_REDIS_PASSWORD" not in vals
    assert local_gateway.roles.gateway.password not in "".join(vals.values())


def test_local_plane_never_serves_the_provider_runner_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_gateway: Any
) -> None:
    """A stale AVA_RUNNER_DB_PASSWORD in a local home's .env is never served:
    the ledger is the local plane's only runner credential."""
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    vals = _projected(monkeypatch, tmp_path)
    assert "AVA_RUNNER_DB_PASSWORD" not in vals
    assert urlsplit(vals["AVA_DB_URL"]).password == local_gateway.roles.runner.password


def test_local_plane_without_a_ledger_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local home with no database authority ledger has nothing to serve; the
    operator is told how to birth or convert it, not handed a dead URL."""
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    with pytest.raises(ValueError, match="no database authority ledger"):
        _projected(monkeypatch, tmp_path)


@pytest.mark.parametrize("role", [None, "runner"])
@pytest.mark.parametrize("missing_url", [None, ""])
def test_missing_url_refuses_before_boot_time_fallback(
    monkeypatch: pytest.MonkeyPatch, role: str | None, missing_url: str | None
) -> None:
    snapshot = {"AVA_RUNNER_DB_PASSWORD": "new-private-password"}
    if missing_url is not None:
        snapshot["AVA_DB_URL"] = missing_url
    read = Mock(return_value=snapshot)
    fallback = Mock(side_effect=AssertionError("must not read a stale Settings URL"))
    warning = Mock()
    monkeypatch.setattr(rt, "read_env_aliases", read)
    monkeypatch.setattr("shared.config.service_read._service_field_value", fallback)
    monkeypatch.setattr("shared.log.logger.warning", warning)
    with pytest.raises(ValueError, match="AVA_DB_URL is missing") as caught:
        config.bootstrap_config_values(role=role)
    read.assert_called_once_with()
    fallback.assert_not_called()
    warning.assert_not_called()
    assert "new-private-password" not in str(caught.value)


def test_remote_plane_projects_the_provider_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider_url = _pg_url("provider-pw", host="db.provider.example:5432")
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    env_path = tmp_path / ".env"
    env_path.write_text(env_path.read_text().replace(_DB_URL, provider_url))
    monkeypatch.setattr(config.settings.data_plane, "db_url", provider_url)
    vals = _projected(monkeypatch, tmp_path)
    parts = urlsplit(vals["AVA_DB_URL"])
    assert (parts.username, parts.password, parts.hostname) == (
        "ava_runner",
        _RUNNER_PW,
        "db.provider.example",
    )
    assert "AVA_RUNNER_DB_PASSWORD" not in vals


def test_remote_plane_without_provider_credential_names_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a remote-managed data plane the runner role is provisioned at the
    provider, so the missing-credential error points there (QA P2, Task #1752)."""
    _write_gateway_env(tmp_path, runner_pw=None)
    env_path = tmp_path / ".env"
    # Built from parts so no `scheme://user:password@host` literal sits in the
    # source for a secret scanner to flag (repo convention).
    provider_url = _pg_url("provider-pw", host="db.provider.example:5432")
    env_path.write_text(env_path.read_text().replace(_DB_URL, provider_url))
    monkeypatch.setattr(config.settings.data_plane, "db_url", provider_url)
    with pytest.raises(ValueError, match="provisioned at the"):
        _projected(monkeypatch, tmp_path)


@pytest.mark.usefixtures("local_gateway")
def test_unknown_role_value_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    with pytest.raises(ValueError, match="not a known projection"):
        config.bootstrap_config_values(role="admin")


@pytest.mark.usefixtures("local_gateway")
def test_runner_projection_excludes_pitr_enablement_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The physical-backup enablement flags are gateway-local facts. When the
    activation wrote them into the gateway .env they rode the bootstrap
    payload to agent-runners, which built PhysicalBackupSettings with
    pitr_enabled=True but held no gateway-local GCS credentials — every
    runner service then refused to start (2026-08-30 incident). Whatever the
    gateway's .env says, these flags must never reach a runner."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        env_path.read_text()
        + "AVA_PITR_ENABLED=true\n"
        + "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        + "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        + "AVA_PITR_RETENTION_PLANNER_ENABLED=true\n"
    )
    vals = _projected(monkeypatch, tmp_path)
    for alias in (
        "AVA_PITR_ENABLED",
        "AVA_PITR_BASE_BACKUP_ENABLED",
        "AVA_PITR_RESTORE_PROOF_ENABLED",
        "AVA_PITR_RETENTION_PLANNER_ENABLED",
    ):
        assert alias not in vals
