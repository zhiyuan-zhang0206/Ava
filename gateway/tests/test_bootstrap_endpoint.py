"""Contract tests for GET /api/bootstrap.

With a cluster secret set, the endpoint requires it as a bearer token — it
serves cluster secrets, so reachability is never trust. A no-secret cluster
(empty secret) serves unauthenticated: it has no credential to present and no
remote runner to protect (the gateway then binds loopback). A single box never
dials it; only an enrolling agent-runner does, and it presents the secret.
"""

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from base import config
from base.cluster.auth import bearer_header
from base.host.env import runtime_config as rt
from gateway.app import app

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture, not a real secret


def _auth() -> dict[str, str]:
    return bearer_header(_SECRET)


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_returns_config_with_secret(db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200
    body = cast(dict[str, str], resp.json())
    # The DB URL is the credential-free endpoint; only the host may be
    # rewritten for a remote runner.
    expected = str(config.settings.data_plane.db_url)
    reachable = config._self_machine_host()
    if not config.is_loopback_host(reachable):
        expected = config.url_with_host(expected, reachable)
    served, owner = urlsplit(body["AVA_DB_URL"]), urlsplit(expected)
    assert (served.username, served.password) == (owner.username, None)
    assert served.hostname == owner.hostname
    assert served.port == owner.port
    assert served.path == owner.path
    assert "AVA_REDIS_URL" in body
    assert "AVA_REDIS_ADMIN_PASSWORD" not in body
    assert "AVA_REDIS_PASSWORD" not in body


@pytest.mark.usefixtures("served_gateway_home")
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


@pytest.mark.usefixtures("served_gateway_home")
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


def test_old_role_runner_request_gets_no_database_login(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    served_gateway_home: Any,
) -> None:
    """Regression: a runner still on the retired exchange asks `?role=runner`
    with the bearer. It receives the credential-free endpoint, never the active
    generation's login, so a stale runner cannot re-authorize itself here."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", params={"role": "runner"}, headers=_auth())
    assert resp.status_code == 200
    body: dict[str, str] = resp.json()
    assert "AVA_RUNNER_DB_PASSWORD" not in body
    parts = urlsplit(body["AVA_DB_URL"])
    assert parts.password is None
    payload = "".join(body.values())
    for role in (served_gateway_home.roles.runner, served_gateway_home.roles.gateway):
        assert role.name not in payload
        assert role.password not in payload


def test_bootstrap_without_a_database_endpoint_refused(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A gateway config snapshot without AVA_DB_URL is a 400, never a guess."""
    (tmp_path / ".env").write_text("AVA_REDIS_URL=redis://127.0.0.1:1/0\n")
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 400
    assert "AVA_DB_URL is missing" in resp.json()["detail"]


def test_bootstrap_strips_a_remote_managed_planes_provider_password(
    db_conn, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote-managed plane's gateway `.env` holds the provider's owner URL,
    password included (a local plane's holds the credential-free endpoint, so it
    cannot tell). Bootstrap serves the endpoint and never that password."""
    provider_password = "provider-" + "p" * 24
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL=postgresql://ava_owner:{provider_password}@db.example.com:5432/ava\n"
        "AVA_REDIS_URL=redis://ava:runtime@redis.example.com:6379/0\n"
    )
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    monkeypatch.setattr(type(config.settings.data_plane), "is_remote", property(lambda _s: True))
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200, resp.text
    served = urlsplit(cast(dict[str, str], resp.json())["AVA_DB_URL"])
    assert (served.username, served.password) == ("ava_owner", None)
    assert (served.hostname, served.port, served.path) == ("db.example.com", 5432, "/ava")
    assert provider_password not in resp.text
