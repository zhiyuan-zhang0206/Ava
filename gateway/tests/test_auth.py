"""Contract tests for cluster-secret + cookie session auth.

The auth middleware gates every API route except /api/health, /api/auth/login,
and /api/auth/check. Two auth methods are accepted:
1. Session cookie (``ava_session``) — for browser users.
2. ``Authorization: Bearer <secret>`` — for SDK / agent callers.

An empty cluster secret is the no-auth posture: the middleware passes every
request through and the app starts without one (single-box clusters birth
secret-less by default; the gateway then binds loopback only).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

import gateway.app as gateway_app
from base import config
from base.cluster.auth import (
    bearer_header,
    cookie_name,
)
from base.config.gateway import GatewaySettings
from gateway.app import app
from gateway.auth.cors import cors_allowed_origins

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def _auth() -> dict[str, str]:
    return bearer_header(_SECRET)


def _session_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{cookie_name()}={token}"}


def _request_with_origin(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"origin", origin.encode())],
        }
    )


@contextmanager
def _rebuilt_cors_middleware() -> Generator[None, None, None]:
    """Swap the app's CORS middleware for one built from the CURRENT settings.

    CORSMiddleware captures ``allow_origins`` when the middleware stack is
    built, and the stack is built once and cached on first request. A preflight
    test that patches gateway settings must therefore rebuild the CORS entry
    and reset the stack (restoring both afterwards), or it keeps exercising the
    import-time allowlist instead of the patched one.
    """
    from starlette.middleware.cors import CORSMiddleware

    original_entry = None
    for i, mw in enumerate(app.user_middleware):
        if mw.cls is CORSMiddleware:
            original_entry = app.user_middleware[i]
            app.user_middleware[i] = Middleware(
                CORSMiddleware,
                allow_origins=cors_allowed_origins(),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            break
    assert original_entry is not None, "gateway app must register CORSMiddleware"
    app.middleware_stack = None
    try:
        yield
    finally:
        for i, mw in enumerate(app.user_middleware):
            if mw.cls is CORSMiddleware:
                app.user_middleware[i] = original_entry
                break
        app.middleware_stack = None


def _login(client: TestClient, *, user_agent: str = "test-browser") -> str:
    response = client.post(
        "/api/auth/login",
        json={"password": _SECRET},
        headers={"User-Agent": user_agent},
    )
    assert response.status_code == 200
    return response.cookies[cookie_name()]


def _legacy_hmac_cookie() -> str:
    """One valid pre-change cookie, built independently of production code."""
    expiry = str(int(time.time()) + 3600).encode()
    mac = hmac.new(_SECRET.encode(), expiry, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(expiry + b"." + mac).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable auth with a known secret for every test in this module.

    The root `_clean_state` disables the middleware (auth_middleware_enabled=false)
    for the rest of the suite; the auth contract tests re-enable it here and set a
    real secret so the middleware actually runs.
    """
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    getattr(gateway_app, "_session_last_touch", {}).clear()


# ── Gateway security settings ─────────────────────────────────────────


def test_cors_allowed_origins_parses_comma_separated_value() -> None:
    parsed = GatewaySettings.model_validate(
        {"cors_allowed_origins": ("https://one.example, http://two.example:3000")}
    )

    assert parsed.cors_allowed_origins == [
        "https://one.example",
        "http://two.example:3000",
    ]


def test_session_cookie_secure_defaults_to_none() -> None:
    assert GatewaySettings().session_cookie_secure is None


def test_session_cookie_secure_parses_explicit_bool() -> None:
    parsed = GatewaySettings.model_validate({"session_cookie_secure": "true"})

    assert parsed.session_cookie_secure is True


def test_cors_allowed_origins_derive_frontend_hosts_and_gateway_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived allowlist mirrors the frontend origins, the gateway's OWN
    origin — its own port, not the frontend entry port — and the frontend
    entry on the gateway host. Regression guard for the /grafana proxy 403:
    the browser's same-origin POST Origin (http://<gateway-host>:<gateway-port>)
    must be allowlisted exactly, and so must the Gate UI's login origin
    (http://<gateway-host>:<frontend-port>).
    """
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "https://gateway.example:8100",
    )

    assert cors_allowed_origins() == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://gateway.example:8100",
        "https://gateway.example:3000",
    ]


def test_cors_allowed_origins_gateway_origin_matches_prod_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod-shaped gateway URL (explicit non-standard port, so the browser
    Origin carries it) yields exactly that origin in the allowlist — plus the
    frontend entry on the same host, which the Gate login page sends."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3100",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://10.0.0.72:8000",
    )

    assert cors_allowed_origins() == [
        "http://localhost:3100",
        "http://127.0.0.1:3100",
        "http://10.0.0.72:8000",
        "http://10.0.0.72:3100",
    ]


def test_cors_allowed_origins_gateway_default_port_has_bare_and_explicit_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port-less gateway URL must yield the origin WITHOUT a port (what a
    browser serializes for the scheme's default port) and the explicit
    default-port form, since both can appear in an Origin header — and so
    must the frontend entry on the gateway host."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "https://gateway.example",
    )

    assert cors_allowed_origins() == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://gateway.example:443",
        "https://gateway.example",
        "https://gateway.example:3000",
    ]


def test_cors_allowed_origins_ipv6_gateway_host_bracketed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IPv6 gateway host is bracketed in derived origins — the form a
    browser puts in the Origin header."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://[2606:4700:4700::1111]:8000",
    )

    assert cors_allowed_origins() == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[2606:4700:4700::1111]:8000",
        "http://[2606:4700:4700::1111]:3000",
    ]


def test_cors_allowed_origins_frontend_default_port_has_bare_and_explicit_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port-less frontend healthcheck URL resolves to the scheme's default
    port; the frontend entries on the gateway host then carry BOTH the bare
    and explicit :80 forms, symmetric with the gateway's own origin handling."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://gateway.example:8000",
    )

    assert cors_allowed_origins() == [
        "http://localhost:80",
        "http://127.0.0.1:80",
        "http://gateway.example:8000",
        "http://gateway.example:80",
        "http://gateway.example",
    ]


def test_cors_allowed_origins_use_explicit_list_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = ["https://one.example", "http://two.example:3000"]
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", explicit)
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:4100",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "https://gateway.example:8100",
    )

    assert cors_allowed_origins() == explicit


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


def test_login_creates_current_server_side_session() -> None:
    with TestClient(app) as client:
        token = _login(client, user_agent="session-list-test")
        resp = client.get("/api/auth/sessions")

    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 1
    assert sessions[0]["id"] == token
    assert sessions[0]["revoked_at"] is None
    assert sessions[0]["user_agent"] == "session-list-test"
    assert sessions[0]["ip"] == "testclient"
    assert sessions[0]["current"] is True


def test_sessions_list_masks_non_current_ids() -> None:
    """Only the request's current session keeps its full id; other rows show
    the final 8 characters (the suffix the revoke endpoint accepts)."""
    with TestClient(app) as client:
        current = _login(client)
        client.cookies.clear()
        other = _login(client, user_agent="other-browser")
        listed = client.get("/api/auth/sessions")

    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 2
    by_current = {row["current"]: row for row in rows}
    assert by_current[True]["id"] == other  # the request's own cookie
    assert by_current[False]["id"] == current[-8:]
    assert by_current[False]["id"] != current  # full id never leaks for others


def test_sessions_list_labels_managed_browser_sessions() -> None:
    """The managed-browser daemon's login UA marks its session rows."""
    with TestClient(app) as client:
        _login(client, user_agent="ava-managed-browser")
        client.cookies.clear()
        _login(client, user_agent="test-browser")
        rows = client.get("/api/auth/sessions").json()

    managed = [row for row in rows if row["user_agent"] == "ava-managed-browser"]
    normal = [row for row in rows if row["user_agent"] == "test-browser"]
    assert len(managed) == 1 and len(normal) == 1
    assert managed[0]["managed"] is True
    assert normal[0]["managed"] is False


def test_revoke_endpoint_accepts_masked_suffix() -> None:
    """The sessions list shows masked ids; revoking one must still work."""
    with TestClient(app) as client:
        current = _login(client)
        client.cookies.clear()
        target = _login(client)
        client.cookies.clear()
        headers = _session_cookie(current)

        listed = client.get("/api/auth/sessions", headers=headers).json()
        target_row = next(row for row in listed if not row["current"] and row["id"] == target[-8:])
        revoke = client.post(f"/api/auth/sessions/{target_row['id']}/revoke", headers=headers)
        replay = client.get("/api/agents", headers=_session_cookie(target))

    assert revoke.status_code == 200
    assert replay.status_code == 401


def test_revoke_endpoint_refuses_ambiguous_suffix(db_conn: psycopg.Connection) -> None:
    """A suffix shared by more than one active session is refused, not revoked
    wholesale."""
    with TestClient(app) as client:
        _login(client)
        with db_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                [("first-12345678",), ("second-12345678",)],
            )
        db_conn.commit()
        response = client.post("/api/auth/sessions/12345678/revoke")

    assert response.status_code == 409
    assert response.json()["detail"] == "session suffix is ambiguous"


def test_revoke_endpoint_short_suffix_returns_404() -> None:
    """Shorter-than-masked inputs are neither full ids nor valid suffixes."""
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/auth/sessions/abcdefg/revoke")

    assert response.status_code == 404


def test_revoke_endpoint_refuses_current_session_by_suffix() -> None:
    """The current session's own masked suffix must not bypass the logout-only
    guard: it 404s and the session stays valid (QA nit1 regression)."""
    with TestClient(app) as client:
        current = _login(client)
        response = client.post(f"/api/auth/sessions/{current[-8:]}/revoke")
        check = client.get("/api/auth/check")

    assert response.status_code == 404
    assert check.json()["authenticated"] is True


def test_revoke_endpoint_ambiguous_suffix_excludes_current_session(
    db_conn: psycopg.Connection,
) -> None:
    """The current session is filtered out of suffix matches BEFORE ambiguity
    is judged: current + two others sharing a suffix still 409s on the two."""
    with TestClient(app) as client:
        current = _login(client)
        suffix = current[-8:]
        with db_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                [(f"other-one-{suffix}",), (f"other-two-{suffix}",)],
            )
        db_conn.commit()
        response = client.post(f"/api/auth/sessions/{suffix}/revoke")
        check = client.get("/api/auth/check")

    assert response.status_code == 409
    assert response.json()["detail"] == "session suffix is ambiguous"
    assert check.json()["authenticated"] is True  # current session untouched


def test_revoke_endpoint_rejects_longer_than_masked_suffix(
    db_conn: psycopg.Connection,
) -> None:
    """Only the exact 8-character masked form falls back to suffix matching; a
    longer non-full-id string must not revoke by suffix (QA nit2 regression)."""
    with TestClient(app) as client:
        _login(client)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                ("abcdefghij",),
            )
        db_conn.commit()
        # "bcdefghij" is a 9-char suffix of an active session but not a full id.
        response = client.post("/api/auth/sessions/bcdefghij/revoke")

    assert response.status_code == 404
    with db_conn.cursor() as cur:
        cur.execute("SELECT revoked_at FROM web_sessions WHERE id = %s", ("abcdefghij",))
        row = cur.fetchone()
    assert row is not None and row[0] is None  # untouched by the 404 revoke


def test_login_sets_cookie_flags_and_configured_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "session_ttl_seconds", 123)

    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})

    set_cookie = resp.headers["Set-Cookie"]
    assert set_cookie.startswith(f"{cookie_name()}=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=123" in set_cookie


def test_login_cookie_secure_derives_from_gateway_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "https://gateway.example:8000")

    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})

    assert "; Secure" in resp.headers["Set-Cookie"]


def test_https_browser_entry_login_preserves_authenticated_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "browser_origin", "https://console.example")
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://192.0.2.2:20016")
    monkeypatch.setattr(config.settings.gateway, "session_cookie_secure", None)
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    with _rebuilt_cors_middleware(), TestClient(app, base_url="https://console.example") as client:
        response = client.post(
            "/api/auth/login",
            json={"password": _SECRET},
            headers={"Origin": "https://console.example"},
        )
        assert response.status_code == 200
        assert "; Secure" in response.headers["Set-Cookie"]
        assert client.get("/api/auth/check").json()["authenticated"] is True


def test_login_cookie_not_secure_for_http_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "session_cookie_secure", None)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://gateway.example:8000")

    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})

    assert "; Secure" not in resp.headers["Set-Cookie"]


def test_login_cookie_secure_when_policy_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "session_cookie_secure", True)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://gateway.example:8000")

    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})

    assert "; Secure" in resp.headers["Set-Cookie"]


def test_login_cookie_secure_for_https_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "session_cookie_secure", None)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "https://gateway.example:8000")

    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"password": _SECRET})

    assert "; Secure" in resp.headers["Set-Cookie"]


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


def test_logout_revokes_server_side_and_clears_cookie() -> None:
    with TestClient(app) as client:
        token = _login(client)
        assert client.get("/api/agents").status_code == 200
        resp = client.post("/api/auth/logout")
        replay = client.get("/api/agents", headers=_session_cookie(token))

    assert resp.status_code == 200
    set_cookie = resp.headers["Set-Cookie"]
    assert "Max-Age=0" in set_cookie or "Expires" in set_cookie
    assert replay.status_code == 401


def test_check_returns_true_with_valid_cookie() -> None:
    with TestClient(app) as client:
        token = _login(client)
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


def test_check_returns_false_with_expired_cookie(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-session",),
        )
    db_conn.commit()

    with TestClient(app) as client:
        resp = client.get(
            "/api/auth/check",
            headers=_session_cookie("expired-session"),
        )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False
