"""Server-side browser-session authentication and revocation endpoints.

POST /api/auth/login   — verify password, set session cookie
POST /api/auth/logout  — revoke and clear the current session cookie
GET  /api/auth/check   — report whether the current session is valid
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway._cors import session_cookie_secure
from gateway.error_envelope import error_response
from gateway.session_store import (
    create_session,
    list_sessions,
    revoke_session,
    session_is_valid,
)
from shared.cluster_auth import (
    clear_cookie_header,
    cookie_name,
    new_session_id,
    session_cookie_header,
)
from shared.config import settings
from shared.rate_limit import login_limiter

router = APIRouter()


class LoginRequest(BaseModel):
    """POST /api/auth/login body. `password` is the cluster secret; a missing or
    empty one falls through to the 401 below rather than a 422. `username` is
    accepted for Chrome password-manager compatibility but never validated."""

    password: str = ""
    username: str | None = None


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with the cluster secret and receive a session cookie.

    Request body: ``{"password": "<cluster-secret>"}``

    On success, returns ``{"ok": true}`` and sets an HTTP-only session
    cookie whose lifetime is controlled by ``session_ttl_seconds``.

    On failure, returns a typed 401 error envelope with detail ``"invalid password"``.

    A no-secret cluster (single-box no-auth posture) has no credential: login
    is a no-op 200 success, deliberately BEFORE the rate limiter — there is
    nothing to guess, and no-secret requests must never count into the
    limiter's failure records.

    Brute-force guard: an IP that fails ``MAX_FAILURES`` times in a row is
    locked for ``LOCKOUT_SECONDS`` (policy + rationale in shared/rate_limit.py).
    While locked, the endpoint returns 429 + ``Retry-After`` instead of 401 —
    401 would read as "wrong password" and invite exactly the retry loop the
    lockout exists to stop. A successful login resets the IP's counter.
    """
    ip = request.client.host if request.client else "unknown"
    secret = settings.data_plane.cluster_secret

    if not secret:
        return JSONResponse(content={"ok": True})

    remaining = login_limiter.lockout_remaining(ip)
    if remaining > 0:
        return error_response(
            request,
            code="login_rate_limited",
            status=429,
            detail="too many failed login attempts",
            retryable=True,
            extensions={"retry_after_seconds": remaining},
            headers={"Retry-After": str(remaining)},
        )

    password = body.password

    if not password:
        login_limiter.record_failure(ip)
        return error_response(
            request,
            code="invalid_password",
            status=401,
            detail="invalid password",
            retryable=False,
        )

    if not hmac.compare_digest(password, secret):
        login_limiter.record_failure(ip)
        return error_response(
            request,
            code="invalid_password",
            status=401,
            detail="invalid password",
            retryable=False,
        )

    login_limiter.record_success(ip)
    session_id = new_session_id()
    ttl_seconds = settings.gateway.session_ttl_seconds
    await asyncio.to_thread(
        create_session,
        request.app.state.db_pool,
        session_id,
        ttl_seconds,
        request.headers.get("user-agent", ""),
        ip,
    )
    headers = session_cookie_header(
        session_id,
        secure=session_cookie_secure(),
        ttl_seconds=ttl_seconds,
    )
    return JSONResponse(content={"ok": True}, headers=headers)


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke and clear the current session cookie; repeated calls are safe."""
    session_id = request.cookies.get(cookie_name())
    if session_id:
        await asyncio.to_thread(revoke_session, request.app.state.db_pool, session_id)
    return JSONResponse(
        content={"ok": True},
        headers=clear_cookie_header(),
    )


@router.get("/api/auth/check")
async def check(request: Request) -> JSONResponse:
    """Report whether the request carries a valid session cookie.

    Returns ``{"authenticated": true}`` or ``{"authenticated": false}``.
    Always 200 — the caller uses the body, not the status code.
    """
    # When auth is disabled (e2e) OR the cluster has no secret, report
    # authenticated so the frontend AuthGuard renders the app instead of
    # redirecting to /login. The session cookie cannot be carried here in e2e:
    # the browser page is on a different host than the gateway, so a
    # SameSite=Lax cookie is dropped on the cross-site check request. A
    # no-secret cluster has no credential to verify, so there is nothing to
    # check.
    if not settings.gateway.auth_middleware_enabled or not settings.data_plane.cluster_secret:
        return JSONResponse(content={"authenticated": True})
    token = request.cookies.get(cookie_name())
    authenticated = await asyncio.to_thread(
        session_is_valid,
        request.app.state.db_pool,
        token,
    )
    return JSONResponse(content={"authenticated": authenticated})


@router.get("/api/auth/sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    """List active browser sessions, marking the request's current cookie."""
    current_session_id = request.cookies.get(cookie_name())
    rows = await asyncio.to_thread(list_sessions, request.app.state.db_pool)
    return [{**row, "current": row["id"] == current_session_id} for row in rows]


@router.post("/api/auth/sessions/{session_id}/revoke")
async def revoke_other_session(session_id: str, request: Request) -> JSONResponse:
    """Revoke a non-current browser session."""
    if session_id == request.cookies.get(cookie_name()):
        raise HTTPException(
            status_code=409,
            detail="current session must be revoked via logout",
        )
    revoked = await asyncio.to_thread(
        revoke_session,
        request.app.state.db_pool,
        session_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})
