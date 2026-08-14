"""Contract tests for cluster-secret + cookie session auth.

The auth middleware gates every API route except /api/health, /api/auth/login,
and /api/auth/check. Three auth methods are accepted:
1. Session cookie (``ava_session``) — for browser users.
2. ``Authorization: Bearer <secret>`` — for SDK / agent callers.
3. ``X-Cluster-Secret: <secret>`` — backward compat bare header.

An empty cluster secret is the no-auth posture: the middleware passes every
request through and the app starts without one (single-box clusters birth
secret-less by default; the gateway then binds loopback only).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config
from shared.cluster_auth import (
    bearer_header,
    cookie_name,
    sign_session,
    verify_session,
)

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def _auth() -> dict[str, str]:
    return bearer_header(_SECRET)


def _x_cluster_secret_header() -> dict[str, str]:
    return {"X-Cluster-Secret": _SECRET}


def _session_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{cookie_name()}={token}"}


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable auth with a known secret for every test in this module.

    The root `_clean_state` disables the middleware (auth_middleware_enabled=false)
    for the rest of the suite; the auth contract tests re-enable it here and set a
    real secret so the middleware actually runs.
    """
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)


# ── Bypass paths are exempt ──────────────────────────────────────────


def test_health_no_auth_returns_200() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200


def test_auth_login_no_auth_allowed() -> None:
    """Login endpoint must be accessible without auth so the browser can
    obtain a session cookie."""
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_auth_check_no_auth_allowed() -> None:
    """Check endpoint returns false, not 401, when no cookie present."""
    with TestClient(app) as client:
        resp = client.get("/api/auth/check")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


# ── Login / logout / check ───────────────────────────────────────────


def test_login_wrong_password_returns_401() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


def test_login_empty_password_returns_401() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": ""})
    assert resp.status_code == 401


def test_login_sets_session_cookie() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})
    assert resp.status_code == 200
    assert "Set-Cookie" in resp.headers
    set_cookie = resp.headers["Set-Cookie"]
    assert set_cookie.startswith(f"{cookie_name()}=")
    assert "HttpOnly" in set_cookie
    # Persistence: without Max-Age Chromium drops the cookie on restart (desktop #706).
    assert "Max-Age=" in set_cookie


def test_login_with_username_succeeds() -> None:
    """Username is accepted for Chrome password-manager compat but not validated."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": _SECRET},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_login_with_wrong_password_but_username_fails() -> None:
    """Only password is checked — username is cosmetic."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
    assert resp.status_code == 401


def test_logout_clears_cookie() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert "Set-Cookie" in resp.headers
    set_cookie = resp.headers["Set-Cookie"]
    assert "Max-Age=0" in set_cookie or "Expires" in set_cookie


def test_check_returns_true_with_valid_cookie() -> None:
    token = sign_session(_SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/auth/check", headers=_session_cookie(token))
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_check_returns_false_with_invalid_cookie() -> None:
    with TestClient(app) as client:
        resp = client.get(
            "/api/auth/check",
            headers=_session_cookie("garbage-token"),
        )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


def test_check_returns_true_when_auth_middleware_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With auth_middleware_enabled=false, check reports authenticated without a cookie —
    the e2e browser is on a different host so a SameSite=Lax cookie cannot
    reach the cross-site check request."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/api/auth/check")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_check_returns_false_with_expired_cookie() -> None:
    # Create a token that's already expired
    token = sign_session(_SECRET, ttl=-1)
    with TestClient(app) as client:
        resp = client.get("/api/auth/check", headers=_session_cookie(token))
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


# ── Unauthenticated requests get 401 ──────────────────────────────────


def test_api_agents_rejects_no_auth() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents")
    assert resp.status_code == 401


def test_api_status_rejects_no_auth() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/status")
    assert resp.status_code == 401


def test_api_bootstrap_rejects_no_auth() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap")
    assert resp.status_code == 401


# ── CORS preflight is exempt from auth ────────────────────────────────


def test_cors_preflight_on_protected_route_not_401() -> None:
    """A browser CORS preflight (OPTIONS) carries no credentials by spec, so
    it can never pass the cookie/Bearer check. The auth middleware must let it
    fall through to CORSMiddleware; otherwise the preflight 401s, never gets
    Access-Control-* headers, and the real cross-origin POST surfaces in the
    browser as "Failed to fetch". Regression guard for the resurrect /
    terminate / restart lifecycle buttons."""
    with TestClient(app) as client:
        resp = client.options(
            "/api/agents/405/resurrect",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert resp.status_code != 401
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


# ── Bearer token works ────────────────────────────────────────────────


def test_api_agents_accepts_bearer() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_auth())
    assert resp.status_code == 200


def test_api_bootstrap_accepts_bearer() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200


# ── X-Cluster-Secret header works ─────────────────────────────────────


def test_api_agents_accepts_x_cluster_secret() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_x_cluster_secret_header())
    assert resp.status_code == 200


# ── Session cookie works on protected routes ──────────────────────────


def test_api_agents_accepts_valid_cookie() -> None:
    token = sign_session(_SECRET)
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_session_cookie(token))
    assert resp.status_code == 200


def test_api_agents_rejects_invalid_cookie() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_session_cookie("bad-token"))
    assert resp.status_code == 401


# ── Wrong secret gets 401 ─────────────────────────────────────────────


def test_api_agents_rejects_wrong_bearer_secret() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=bearer_header("wrong-secret"))
    assert resp.status_code == 401


def test_api_agents_rejects_wrong_x_cluster_secret() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers={"X-Cluster-Secret": "wrong-secret"})
    assert resp.status_code == 401


# ── Empty secret = no auth (the no-secret cluster posture) ─────────────


def test_empty_secret_serves_without_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty cluster secret IS the no-auth state (user decision: off is fully
    off): the middleware passes every request through, and the app starts with
    no secret at all. A no-secret cluster binds loopback only (see `main()`),
    so this never exposes an unauthenticated API to the LAN."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app) as client:
        resp = client.get("/api/agents")
    assert resp.status_code == 200


def test_gateway_starts_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-secret startup is legal: the lifespan no longer refuses to serve an
    unauthenticated API (that is now a first-class configuration)."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app) as client:
        resp = client.get("/api/status")
    assert resp.status_code == 200


def test_gateway_starts_when_auth_disabled_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The e2e knob: auth_middleware_enabled=false lets requests pass while the
    cluster keeps its secret (for internal service-to-service auth)."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/api/agents")
    assert resp.status_code == 200


def test_check_reports_authenticated_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-secret cluster has no credential to verify: /api/auth/check reports
    authenticated so the frontend AuthGuard renders the app instead of sending
    the user to a login page that cannot succeed."""
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app) as client:
        resp = client.get("/api/auth/check")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_login_succeeds_without_secret_and_bypasses_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-secret cluster accepts login as a no-op success. The rate limiter
    is bypassed entirely — there is no credential to guess, and no-secret
    requests must never count into its failure records."""
    from shared.rate_limit import login_limiter

    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    records: list[str] = []

    def _record_failure(ip: str) -> None:
        records.append(ip)

    monkeypatch.setattr(login_limiter, "record_failure", _record_failure)
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert records == [], "no-secret login must not touch the limiter's failure counter"


# ── Cookie signing is verifiable ──────────────────────────────────────


def test_sign_and_verify_roundtrip() -> None:
    token = sign_session(_SECRET)
    assert verify_session(token, _SECRET) is True


def test_verify_rejects_wrong_secret() -> None:
    token = sign_session(_SECRET)
    assert verify_session(token, "different-secret") is False


def test_verify_rejects_expired_token() -> None:
    token = sign_session(_SECRET, ttl=-1)
    assert verify_session(token, _SECRET) is False


def test_verify_rejects_empty_token() -> None:
    assert verify_session("", _SECRET) is False
    assert verify_session(None, _SECRET) is False
