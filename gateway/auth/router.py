"""Server-side browser-session authentication and revocation endpoints.

POST /api/auth/login   — verify password, set session cookie
POST /api/auth/logout  — revoke and clear the current session cookie
GET  /api/auth/check   — report whether the current session is valid
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from base.cluster.auth import (
    clear_cookie_header,
    cookie_name,
    is_managed_browser_user_agent,
    session_cookie_header,
)
from base.cluster.rate_limit import login_limiter
from base.config import settings
from gateway.auth.cors import session_cookie_secure
from gateway.auth.request_principal import (
    SessionKeys,
    current_session_fact,
    login_mint,
    session_mints,
)
from gateway.auth.session_store import (
    list_sessions,
    minted_session_id,
    session_ids_with_suffix,
    session_mint,
)
from gateway.middleware.error_envelope import error_response

router = APIRouter()


class LoginRequest(BaseModel):
    """POST /api/auth/login body. `password` is the cluster secret (or, for the
    managed browser of an agent-runner, the active generation's runner API
    token); a missing or empty one falls through to the 401 below rather than a
    422. `username` is accepted for Chrome password-manager compatibility but
    never validated."""

    password: str = ""
    username: str | None = None


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with the cluster secret and receive a session cookie.

    Request body: ``{"password": "<cluster-secret>"}``. An agent-runner's
    managed browser presents the active generation's runner API token instead.
    The session is bound to the credential that minted it (`login_mint`): it
    stops authenticating when that generation is revoked or the human secret
    rotates, whatever its remaining lifetime.

    On success, returns ``{"ok": true}`` and sets an HTTP-only session
    cookie whose lifetime is controlled by ``session_ttl_seconds``.

    On failure, returns a typed 401 error envelope with detail ``"invalid password"``.

    A no-secret cluster (single-box no-auth posture) has no credential: login
    is a no-op 200 success, deliberately BEFORE the rate limiter — there is
    nothing to guess, and no-secret requests must never count into the
    limiter's failure records.

    Brute-force guard: an IP that fails ``gateway.login_max_failures`` times in
    a row is locked for ``gateway.login_lockout_seconds`` (policy + rationale on
    those config fields; enforcement in base/cluster/rate_limit.py).
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

    mint = await asyncio.to_thread(login_mint, request.app.state.session_keys, password, secret)
    if mint is None:
        login_limiter.record_failure(ip)
        return error_response(
            request,
            code="invalid_password",
            status=401,
            detail="invalid password",
            retryable=False,
        )

    login_limiter.record_success(ip)
    session_id = minted_session_id(mint)
    ttl_seconds = settings.gateway.session_ttl_seconds
    await asyncio.to_thread(
        request.app.state.sessions.create,
        session_id,
        ttl_seconds,
        request.headers.get("user-agent", ""),
        ip,
    )
    headers = session_cookie_header(
        session_id,
        secure=session_cookie_secure(str(request.url)),
        ttl_seconds=ttl_seconds,
    )
    return JSONResponse(content={"ok": True}, headers=headers)


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke and clear the current session cookie; repeated calls are safe."""
    session_id = request.cookies.get(cookie_name())
    if session_id:
        await asyncio.to_thread(request.app.state.sessions.revoke, session_id)
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
    fact = await asyncio.to_thread(
        current_session_fact,
        request.app.state.sessions,
        request.app.state.session_keys,
        token,
        settings.data_plane.cluster_secret,
    )
    return JSONResponse(content={"authenticated": fact is not None})


def _sessions_that_authenticate(pool: Any, keys: SessionKeys, secret: str) -> list[dict[str, Any]]:
    """Unrevoked, unexpired sessions whose mint is still admitted: the rows
    `current_session_fact` would accept. A rotated secret's sessions and ids without
    a mint are dead and omitted."""
    admitted = session_mints(keys, secret)
    return [row for row in list_sessions(pool) if session_mint(row["id"]) in admitted]


@router.get("/api/auth/sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    """List browser sessions that still authenticate, marking the current cookie.

    A session is listed only while the credential that minted it is current,
    the same test the session check applies, so a security screen never shows
    a dead session as active. Only the request's current session keeps its full
    id; every other row's id is masked to its final 8 characters — enough to
    tell rows apart and to revoke (the revoke endpoint accepts the suffix),
    without exposing the full credential of sessions the caller does not hold.
    Managed-browser sessions are labeled with ``managed`` so they are not
    mistaken for the caller's own.
    """
    current_session_id = request.cookies.get(cookie_name())
    rows = await asyncio.to_thread(
        _sessions_that_authenticate,
        request.app.state.db_pool,
        request.app.state.session_keys,
        settings.data_plane.cluster_secret,
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        is_current = row["id"] == current_session_id
        result.append(
            {
                **row,
                "id": row["id"] if is_current else row["id"][-8:],
                "current": is_current,
                "managed": is_managed_browser_user_agent(row["user_agent"]),
            }
        )
    return result


@router.post("/api/auth/sessions/{session_id}/revoke")
async def revoke_other_session(session_id: str, request: Request) -> JSONResponse:
    """Revoke a non-current browser session.

    Accepts either the full session id or the 8-character masked suffix shown
    in the sessions list. The suffix fallback only triggers for the exact
    masked form (8 characters) — any other shape is a full id and 404s when
    unknown — and never matches the request's own session (whose only
    revocation path is logout). An ambiguous suffix (more than one other
    active session ends with it) is refused rather than revoked wholesale.
    """
    current_session_id = request.cookies.get(cookie_name())
    if session_id == current_session_id:
        raise HTTPException(
            status_code=409,
            detail="current session must be revoked via logout",
        )
    pool = request.app.state.db_pool
    revoked = await asyncio.to_thread(request.app.state.sessions.revoke, session_id)
    if not revoked and len(session_id) == 8:
        # The masked suffix from the list — nothing else falls back here. The
        # current session is excluded: its only revocation path is logout, and
        # its own suffix must not bypass that guard.
        matches = [
            match
            for match in await asyncio.to_thread(session_ids_with_suffix, pool, session_id)
            if match != current_session_id
        ]
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail="session suffix is ambiguous",
            )
        if len(matches) == 1:
            revoked = await asyncio.to_thread(request.app.state.sessions.revoke, matches[0])
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})
