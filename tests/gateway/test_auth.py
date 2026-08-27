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

import base64
import hashlib
import hmac
import time

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import gateway.app as gateway_app
from gateway._cors import cors_allowed_origins
from gateway.app import _cors_headers, app
from shared import config
from shared.cluster_auth import (
    bearer_header,
    cookie_name,
)
from shared.config.gateway import GatewaySettings

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def _auth() -> dict[str, str]:
    return bearer_header(_SECRET)


def _x_cluster_secret_header() -> dict[str, str]:
    return {"X-Cluster-Secret": _SECRET}


def _session_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{cookie_name()}={token}"}


def _request_with_origin(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"origin", origin.encode())],
        }
    )


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
        "https://gateway.example:3000",
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
    allowed_origin = cors_allowed_origins()[0]
    with TestClient(app) as client:
        resp = client.options(
            "/api/agents/405/resurrect",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert resp.status_code != 401
    assert resp.headers["access-control-allow-origin"] == allowed_origin


def test_cors_preflight_disallowed_origin_has_no_allow_origin_header() -> None:
    with TestClient(app) as client:
        resp = client.options(
            "/api/agents/405/resurrect",
            headers={
                "Origin": "https://disallowed.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert "access-control-allow-origin" not in resp.headers


def test_unauthorized_response_carries_cors_headers() -> None:
    """A 401 short-circuited by the auth middleware still carries the CORS
    headers — CORSMiddleware is the OUTERMOST middleware, so a browser caller
    sees the real 401 instead of "Failed to fetch" (#187)."""
    allowed_origin = cors_allowed_origins()[0]
    with TestClient(app) as client:
        resp = client.get(
            "/api/agents",
            headers={"Origin": allowed_origin},
        )
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == allowed_origin
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_cors_headers_reflect_allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config.settings.gateway,
        "cors_allowed_origins",
        ["https://allowed.example"],
    )

    assert _cors_headers(_request_with_origin("https://allowed.example")) == {
        "Access-Control-Allow-Origin": "https://allowed.example",
        "Vary": "Origin",
        "Access-Control-Allow-Credentials": "true",
    }


def test_cors_headers_omit_disallowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config.settings.gateway,
        "cors_allowed_origins",
        ["https://allowed.example"],
    )

    assert _cors_headers(_request_with_origin("https://disallowed.example")) == {}


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
    with TestClient(app) as client:
        token = _login(client)
        resp = client.get("/api/agents", headers=_session_cookie(token))
    assert resp.status_code == 200


def test_api_agents_rejects_invalid_cookie() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_session_cookie("bad-token"))
    assert resp.status_code == 401


def test_cookie_authenticated_post_rejects_disallowed_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config.settings.gateway,
        "cors_allowed_origins",
        ["https://allowed.example"],
    )
    with TestClient(app) as client:
        token = _login(client)
        resp = client.post(
            "/api/frontend-telemetry",
            content=b"{}",
            headers={
                **_session_cookie(token),
                "Origin": "https://disallowed.example",
            },
        )

    assert resp.status_code == 403
    assert resp.json() == {"detail": "origin not allowed"}
    assert resp.headers["vary"] == "Origin"


def test_bearer_authenticated_post_allows_disallowed_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config.settings.gateway,
        "cors_allowed_origins",
        ["https://allowed.example"],
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/frontend-telemetry",
            content=b"{}",
            headers={
                **_auth(),
                "Origin": "https://disallowed.example",
            },
        )

    assert resp.status_code == 422


def test_cookie_authenticated_post_allows_missing_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config.settings.gateway,
        "cors_allowed_origins",
        ["https://allowed.example"],
    )
    with TestClient(app) as client:
        token = _login(client)
        resp = client.post(
            "/api/frontend-telemetry",
            content=b"{}",
            headers=_session_cookie(token),
        )

    assert resp.status_code == 422


def test_legacy_self_contained_hmac_cookie_is_rejected() -> None:
    with TestClient(app) as client:
        resp = client.get(
            "/api/agents",
            headers=_session_cookie(_legacy_hmac_cookie()),
        )
    assert resp.status_code == 401


def test_revoke_endpoint_invalidates_another_session() -> None:
    with TestClient(app) as client:
        first = _login(client)
        client.cookies.clear()
        second = _login(client)
        client.cookies.clear()

        revoke = client.post(
            f"/api/auth/sessions/{second}/revoke",
            headers=_session_cookie(first),
        )
        replay = client.get("/api/agents", headers=_session_cookie(second))
        listed = client.get("/api/auth/sessions", headers=_session_cookie(first))

    assert revoke.status_code == 200
    assert replay.status_code == 401
    assert [session["id"] for session in listed.json()] == [first]


def test_revoke_endpoint_refuses_current_session() -> None:
    with TestClient(app) as client:
        current = _login(client)
        response = client.post(f"/api/auth/sessions/{current}/revoke")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "http_409"
    assert body["status"] == 409
    assert body["detail"] == "current session must be revoked via logout"
    assert body["retryable"] is False


def test_revoke_endpoint_returns_404_for_missing_or_revoked_session() -> None:
    with TestClient(app) as client:
        current = _login(client)
        client.cookies.clear()
        target = _login(client)
        client.cookies.clear()
        headers = _session_cookie(current)

        first = client.post(f"/api/auth/sessions/{target}/revoke", headers=headers)
        second = client.post(f"/api/auth/sessions/{target}/revoke", headers=headers)
        missing = client.post("/api/auth/sessions/missing/revoke", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 404
    assert missing.status_code == 404


def test_middleware_touches_session_at_most_once_per_minute(
    db_conn: psycopg.Connection,
) -> None:
    with TestClient(app) as client:
        token = _login(client)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE web_sessions SET last_seen_at = now() - interval '1 day' WHERE id = %s",
                (token,),
            )
        db_conn.commit()

        assert client.get("/api/agents").status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM web_sessions WHERE id = %s", (token,))
            first_touch_row = cur.fetchone()
            assert first_touch_row is not None
            first_touch = first_touch_row[0]
            cur.execute(
                "UPDATE web_sessions SET last_seen_at = now() - interval '1 day' WHERE id = %s",
                (token,),
            )
            cur.execute("SELECT last_seen_at FROM web_sessions WHERE id = %s", (token,))
            reset_touch_row = cur.fetchone()
            assert reset_touch_row is not None
            reset_touch = reset_touch_row[0]
        db_conn.commit()

        assert first_touch > reset_touch
        assert client.get("/api/agents").status_code == 200

    with db_conn.cursor() as cur:
        cur.execute("SELECT last_seen_at FROM web_sessions WHERE id = %s", (token,))
        last_seen_row = cur.fetchone()
        assert last_seen_row is not None
        assert last_seen_row[0] == reset_touch


def test_middleware_bounds_session_touch_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10_000.0
    monkeypatch.setattr(config.settings.gateway, "session_ttl_seconds", 3600)

    with TestClient(app) as client:
        token = _login(client)
        gateway_app._session_last_touch["stale"] = now - 3601
        gateway_app._session_last_touch.update(
            {f"recent-{index}": now - index for index in range(1025)}
        )
        monkeypatch.setattr(gateway_app.time, "monotonic", lambda: now)

        response = client.get("/api/agents")

    assert response.status_code == 200
    assert "stale" not in gateway_app._session_last_touch
    assert token in gateway_app._session_last_touch
    assert len(gateway_app._session_last_touch) == 1024


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
