"""Auth enforcement on protected routes: unauthenticated 401s, CORS preflight exemption, bearer/cookie acceptance, and the empty-secret cluster posture; split from gateway/tests/test_auth.py (task #4922)."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

import gateway.app as gateway_app
from base import config
from base.cluster.auth import bearer_header
from gateway.app import app
from gateway.auth.cors import cors_allowed_origins
from gateway.middleware.error_handlers import cors_headers
from gateway.tests.test_auth import (
    _SECRET,
    _auth,
    _legacy_hmac_cookie,
    _login,
    _rebuilt_cors_middleware,
    _request_with_origin,
    _session_cookie,
)
from gateway.tests.test_auth import _patch_secret as _patch_secret

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


def test_cors_preflight_login_from_gateway_frontend_origin_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Gate UI lives on the gateway host at the frontend port, so its
    login request carries Origin http://<gateway-host>:<frontend-port>.
    Regression guard for the reported 400 "Disallowed CORS origin" on
    OPTIONS /api/auth/login: the derived allowlist used to carry only the
    gateway's own port, never the frontend entry on the gateway host, so the
    preflight was rejected before the browser ever sent the login POST."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://10.0.0.72:8000",
    )
    with _rebuilt_cors_middleware(), TestClient(app) as client:
        resp = client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://10.0.0.72:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code != 400
        assert resp.headers["access-control-allow-origin"] == "http://10.0.0.72:3000"


def test_cookie_authenticated_post_allows_gateway_frontend_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cookie-authenticated state-changing POST from the Gate UI's origin —
    gateway host, frontend port — must pass the exact-origin check, not 403.
    Mirrors the gateway-own-origin test: the allowlist needs BOTH the frontend
    entry and the gateway's own port."""
    monkeypatch.setattr(config.settings.gateway, "cors_allowed_origins", [])
    monkeypatch.setattr(
        config.settings.services,
        "frontend_healthcheck_url",
        "http://localhost:3000",
    )
    monkeypatch.setattr(
        config.settings.gateway,
        "gateway_url",
        "http://10.0.0.72:8000",
    )
    with TestClient(app) as client:
        token = _login(client)
        resp = client.post(
            "/api/frontend-telemetry",
            content=b"{}",
            headers={
                **_session_cookie(token),
                "Origin": "http://10.0.0.72:3000",
            },
        )

    # 422 (malformed batch) not 403 — the frontend-port origin passed the exact-origin check.
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_telemetry_batch"


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

    assert cors_headers(_request_with_origin("https://allowed.example")) == {
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

    assert cors_headers(_request_with_origin("https://disallowed.example")) == {}


# ── Bearer token works ────────────────────────────────────────────────


def test_api_agents_accepts_bearer() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers=_auth())
    assert resp.status_code == 200


def test_api_bootstrap_accepts_bearer() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/bootstrap", headers=_auth())
    assert resp.status_code == 200


# ── X-Cluster-Secret header is rejected ────────────────────────────────


def test_api_agents_rejects_x_cluster_secret() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents", headers={"X-Cluster-Secret": _SECRET})
    assert resp.status_code == 401


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


def test_cookie_authenticated_post_allows_gateway_own_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's own origin — the one a browser page served from the
    gateway (Grafana proxy included) sends on same-origin POSTs — must pass
    the Origin check. Regression guard for the /grafana data POST 403: the
    allowlist previously carried the frontend port instead of the gateway
    port, so every same-origin POST was rejected."""
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
    with TestClient(app) as client:
        token = _login(client)
        resp = client.post(
            "/api/frontend-telemetry",
            content=b"{}",
            headers={
                **_session_cookie(token),
                "Origin": "http://10.0.0.72:8000",
            },
        )

    # 422 (malformed batch) not 403 — the request reached the route: the
    # gateway's own origin passed the exact-origin check.
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_telemetry_batch"


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
    from base.cluster.rate_limit import login_limiter

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
