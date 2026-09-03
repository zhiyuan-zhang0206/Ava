"""Tests for the bootstrap runner-role projection (Task #1236).

`GET /api/bootstrap` always serves the cluster's AVA_DB_URL with the
least-privilege `ava_runner` identity + its own password (carried INSIDE the
URL, never served as a standalone key). Owner credentials never leave the
gateway. A runner request on a cluster without a provisioned runner credential
fails loudly, and an unknown role value is refused.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from shared import config
from shared import runtime_config as rt

_RUNNER_PW = "runner-secret-token"
_DB_URL = "postgresql://ava:mainpw@127.0.0.1:5433/ava"


def _pg_url(pw: str, *, host: str) -> str:
    """A credentialed postgres URL built from parts, so the source carries no
    `scheme://user:password@host` literal for a secret scanner to flag (same
    convention as tests/shared/test_url_secret.py)."""
    return f"postgresql://ava:{pw}@{host}/ava"


def _write_gateway_env(tmp_path: Path, runner_pw: str | None = None) -> None:
    lines = [
        f"AVA_DB_URL={_DB_URL}",
        "AVA_DB_ADMIN_PASSWORD=db-admin-only",
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


def test_no_role_projects_ava_runner_url_and_never_serves_admin_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    vals = config.bootstrap_config_values()
    parts = urlsplit(vals["AVA_DB_URL"])
    assert parts.username == "ava_runner"
    assert parts.password == _RUNNER_PW
    assert "AVA_DB_ADMIN_PASSWORD" not in vals
    assert "AVA_REDIS_ADMIN_PASSWORD" not in vals
    assert "AVA_REDIS_PASSWORD" not in vals


def test_runner_role_projects_ava_runner_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    vals = _projected(monkeypatch, tmp_path)

    parts = urlsplit(vals["AVA_DB_URL"])
    assert parts.username == "ava_runner"
    assert parts.password == _RUNNER_PW
    # host / port / database / query survive verbatim — only the identity swaps.
    assert parts.hostname == "127.0.0.1"
    assert parts.port == 5433
    assert parts.path == "/ava"


@pytest.mark.parametrize("role", [None, "runner"])
@pytest.mark.parametrize("missing_url", [None, ""])
def test_missing_url_refuses_new_password_before_boot_time_fallback(
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


@pytest.mark.parametrize("role", [None, "runner"])
def test_snapshot_projection_preserves_reachable_host_and_new_credential(
    monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    read = Mock(
        return_value={
            "AVA_DB_URL": _DB_URL,
            "AVA_RUNNER_DB_PASSWORD": "new-private-password",
        }
    )
    monkeypatch.setattr(rt, "read_env_aliases", read)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "gateway.example")
    values = config.bootstrap_config_values(role=role)
    parts = urlsplit(values["AVA_DB_URL"])
    assert (parts.username, parts.password, parts.hostname, parts.port, parts.path) == (
        "ava_runner",
        "new-private-password",
        "gateway.example",
        5433,
        "/ava",
    )
    read.assert_called_once_with()


def test_runner_password_never_served_as_standalone_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    vals = _projected(monkeypatch, tmp_path)
    assert "AVA_RUNNER_DB_PASSWORD" not in vals


def test_runner_projection_without_credential_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A runner request on a cluster that never provisioned the role gets the
    operator fix, not a URL that would fail at first connect."""
    _write_gateway_env(tmp_path, runner_pw=None)
    with pytest.raises(ValueError, match="ensure-db-role"):
        _projected(monkeypatch, tmp_path)


def test_runner_projection_without_credential_on_remote_plane_names_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a remote-managed data plane the ensure-db-role self-heal path is
    unavailable (the command refuses remote planes), so the missing-credential
    error must point the operator at the provider-provisioned role instead of a
    command that cannot run (QA P2, Task #1752)."""
    _write_gateway_env(
        tmp_path,
        runner_pw=None,
    )
    # remote URLs in the gateway .env
    env_path = tmp_path / ".env"
    # Built from parts so no `scheme://user:password@host` literal sits in the
    # source for a secret scanner to flag (repo convention).
    provider_url = _pg_url("provider-pw", host="db.provider.example:5432")
    env_path.write_text(env_path.read_text().replace(_DB_URL, provider_url))
    with pytest.raises(ValueError, match="provisioned at the"):
        _projected(monkeypatch, tmp_path)


def test_unknown_role_value_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    with pytest.raises(ValueError, match="not a known projection"):
        config.bootstrap_config_values(role="admin")


def test_runner_projection_excludes_pitr_enablement_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The physical-backup enablement flags are gateway-local facts. When the
    activation wrote them into the gateway .env they rode the bootstrap
    payload to agent-runners, which built PhysicalBackupSettings with
    pitr_enabled=True but held no gateway-local GCS credentials — every
    runner service then refused to start (2026-08-30 incident). Whatever the
    gateway's .env says, these flags must never reach a runner."""
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
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
