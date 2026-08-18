"""Tests for the bootstrap runner-role projection (Task #1236).

`GET /api/bootstrap?role=runner` must serve the cluster's AVA_DB_URL with the
least-privilege `ava_runner` identity + its own password (carried INSIDE the
URL, never served as a standalone key); no role serves the main identity —
the pre-cutover contract, unchanged. A runner request on a cluster without a
provisioned runner credential fails loudly with the fix, and an unknown role
value is refused.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from shared import config
from shared import runtime_config as rt

_RUNNER_PW = "runner-secret-token"
_DB_URL = "postgresql://ava:mainpw@127.0.0.1:5433/ava"


def _write_gateway_env(tmp_path: Path, runner_pw: str | None = None) -> None:
    lines = [f"AVA_DB_URL={_DB_URL}"]
    if runner_pw is not None:
        lines.append(f"AVA_RUNNER_DB_PASSWORD={runner_pw}")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")


def _projected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """bootstrap_config_values with the gateway .env pinned to tmp_path."""
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    return config.bootstrap_config_values(role="runner")


def test_no_role_serves_main_identity_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_gateway_env(tmp_path)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    vals = config.bootstrap_config_values()
    assert vals["AVA_DB_URL"] == _DB_URL


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
    with pytest.raises(ValueError, match="ensure-runner-role"):
        _projected(monkeypatch, tmp_path)


def test_unknown_role_value_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_gateway_env(tmp_path, runner_pw=_RUNNER_PW)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    with pytest.raises(ValueError, match="not a known projection"):
        config.bootstrap_config_values(role="admin")
