"""Contract tests for GET /api/bootstrap.

With a cluster secret set, the endpoint requires it as a bearer token — it
serves cluster secrets, so reachability is never trust. A no-secret cluster
(empty secret) serves unauthenticated: it has no credential to present and no
remote runner to protect (the gateway then binds loopback). A single box never
dials it; only an enrolling agent-runner does, and it presents the secret.
"""

from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config
from shared.cluster_auth import bearer_header

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture, not a real secret


def _auth() -> dict[str, str]:
    return bearer_header(_SECRET)


def test_bootstrap_returns_config_with_secret(db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200
    body = cast(dict[str, str], resp.json())
    # Every projection uses the least-privilege DB role; only the host may be
    # rewritten for a remote runner.
    expected = str(config.settings.data_plane.db_url)
    reachable = config._self_machine_host()
    if not config.is_loopback_host(reachable):
        expected = config.url_with_host(expected, reachable)
    served, owner = urlsplit(body["AVA_DB_URL"]), urlsplit(expected)
    assert served.username == "ava_runner"
    assert served.hostname == owner.hostname
    assert served.port == owner.port
    assert served.path == owner.path
    assert "AVA_REDIS_URL" in body
    assert "AVA_DB_ADMIN_PASSWORD" not in body
    assert "AVA_REDIS_ADMIN_PASSWORD" not in body
    assert "AVA_REDIS_PASSWORD" not in body


def test_bootstrap_carries_no_cluster_name(db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Path-only identity: no name travels in the payload — a runner's cluster
    identity is the gateway URL + secret; the db/role identifiers ride inside
    the connection URLs as data."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200
    assert "AVA_CLUSTER" not in resp.json()


def test_bootstrap_rejects_missing_secret(db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """No bearer -> 401 (reachability is not trust)."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap")
    assert resp.status_code == 401


def test_bootstrap_rejects_wrong_secret(db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=bearer_header("wrong-secret"))
    assert resp.status_code == 401


def test_bootstrap_serves_without_auth_when_secret_unset(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset cluster secret is the no-auth posture: the endpoint serves
    without a bearer (there is no credential to require)."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap")
    assert resp.status_code == 200
    assert "AVA_DB_URL" in resp.json()


def test_bootstrap_role_runner_projects_ava_runner_url(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """?role=runner serves AVA_DB_URL as the least-privilege ava_runner identity
    with its own password, carried inside the URL (Task #1236) — and never as a
    standalone key."""
    from shared import runtime_config as rt

    runner_pw = "runner-endpoint-pw"
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL={config.settings.data_plane.db_url}\nAVA_RUNNER_DB_PASSWORD={runner_pw}\n"
    )
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", params={"role": "runner"}, headers=_auth())
    assert resp.status_code == 200
    body: dict[str, str] = resp.json()
    assert "AVA_RUNNER_DB_PASSWORD" not in body
    parts = urlsplit(body["AVA_DB_URL"])
    assert parts.username == "ava_runner"
    assert parts.password == runner_pw
    assert parts.path == urlsplit(config.settings.data_plane.db_url).path


def test_bootstrap_unknown_role_refused(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", params={"role": "admin"}, headers=_auth())
    assert resp.status_code == 400


def test_bootstrap_role_runner_without_credential_refused(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A runner projection on a cluster that never provisioned the role fails
    with the operator fix, not a URL that dies at first connect."""
    from shared import runtime_config as rt

    (tmp_path / ".env").write_text(f"AVA_DB_URL={config.settings.data_plane.db_url}\n")
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", params={"role": "runner"}, headers=_auth())
    assert resp.status_code == 400
    assert "ensure-db-role" in resp.json()["detail"]
