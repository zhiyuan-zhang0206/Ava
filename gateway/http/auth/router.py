"""Server-side browser-session authentication and revocation endpoints.

POST /api/auth/login               — verify password, set session cookie
POST /api/auth/login-ticket        — issue a one-time, short-lived login ticket
POST /api/auth/login-ticket/redeem — exchange a ticket for a session cookie
POST /api/auth/logout              — revoke and clear the current session cookie
GET  /api/auth/check               — report whether the current session is valid
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from base.cluster.auth import (
    clear_cookie_header,
    cookie_name,
    is_managed_browser_user_agent,
    session_cookie_header,
)
from base.cluster.authority.api import AcceptanceCache
from base.cluster.rate_limit import LoginRateLimiter
from base.config import settings
from gateway.http.auth.request_principal import (
    SessionKeys,
    current_session_fact,
    login_mint,
    require_human_credential,
    session_mints,
)
from gateway.http.auth.session_store import (
    LOGIN_TICKET_TTL_SECONDS,
    list_sessions,
    minted_session_id,
    minted_ticket_id,
    session_ids_with_suffix,
    session_mint,
    ticket_mint,
)
from gateway.http.middleware.cors import session_cookie_secure
from gateway.http.middleware.error_envelope import error_response

router = APIRouter()


class LoginRequest(BaseModel):
    """POST /api/auth/login body. `password` is the cluster secret (or, for the
    managed browser of an agent-runner, the active generation's runner API
    token); a missing or empty one falls through to the 401 below rather than a
    422. `username` is accepted for Chrome password-manager compatibility but
    never validated."""

    password: str = ""
    username: str | None = None


class LoginTicketRedeemRequest(BaseModel):
    """POST /api/auth/login-ticket/redeem body; a missing or empty ticket falls
    through to the 401 rather than a 422, like a missing password."""

    ticket: str = ""


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

    Brute-force guard: an IP that fails ``gateway.login_max_failures`` times in
    a row is locked for ``gateway.login_lockout_seconds`` (policy + rationale on
    those config fields; enforcement in base/cluster/rate_limit.py).
    While locked, the endpoint returns 429 + ``Retry-After`` instead of 401 —
    401 would read as "wrong password" and invite exactly the retry loop the
    lockout exists to stop. A successful login resets the IP's counter.
    """
    ip = request.client.host if request.client else "unknown"
    secret = settings.data_plane.cluster_secret

    login_limiter: LoginRateLimiter = request.app.state.login_limiter
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

    mint = await asyncio.to_thread(
        login_mint,
        request.app.state.session_keys,
        password,
        secret,
        cache=request.app.state.machine_token_acceptance,
    )
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
    return await _open_session(request, mint, ip)


async def _open_session(request: Request, mint: str, ip: str) -> JSONResponse:
    """Create a session bound to `mint` and answer with its cookie."""
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


@router.post("/api/auth/login-ticket", dependencies=[Depends(require_human_credential)])
async def issue_login_ticket(request: Request) -> JSONResponse:
    """Issue a one-time login ticket for a browser, from a human-secret caller.

    The ticket is a `web_sessions` row valid for ``LOGIN_TICKET_TTL_SECONDS``
    and bound to the human secret's mint: redeeming it
    (``/api/auth/login-ticket/redeem``) opens exactly the session a password
    login with that secret would. Only the human secret (or a session it
    minted) may issue one; a write generation's machine token gets 403, so no
    machine credential can mint a browser session.

    Returns ``{"ticket": "<opaque>", "expires_in_seconds": <int>}``.
    """
    secret = settings.data_plane.cluster_secret
    mint = await asyncio.to_thread(
        login_mint,
        request.app.state.session_keys,
        secret,
        secret,
        cache=request.app.state.machine_token_acceptance,
    )
    if mint is None:
        raise RuntimeError("the gateway has no cluster secret to issue a login ticket from")
    ticket = minted_ticket_id(mint)
    ip = request.client.host if request.client else "unknown"
    await asyncio.to_thread(
        request.app.state.sessions.create,
        ticket,
        LOGIN_TICKET_TTL_SECONDS,
        "login-ticket",
        ip,
    )
    return JSONResponse(
        content={"ticket": ticket, "expires_in_seconds": LOGIN_TICKET_TTL_SECONDS},
        headers={"Cache-Control": "no-store"},
    )


async def _consume_login_ticket(request: Request, ticket: str) -> str | None:
    """Spend `ticket` and return its mint, or None when it cannot open a session.

    The revoke is the single-use gate (atomic: it matches only an unrevoked,
    unexpired row); a spent ticket whose mint is no longer admitted (the secret
    rotated, a generation was revoked) is still spent.
    """
    mint = ticket_mint(ticket)
    if mint is None or not await asyncio.to_thread(request.app.state.sessions.revoke, ticket):
        return None
    admitted = await asyncio.to_thread(
        session_mints,
        request.app.state.session_keys,
        settings.data_plane.cluster_secret,
        cache=request.app.state.machine_token_acceptance,
    )
    return mint if mint in admitted else None


@router.post("/api/auth/login-ticket/redeem")
async def redeem_login_ticket(body: LoginTicketRedeemRequest, request: Request) -> JSONResponse:
    """Exchange a login ticket for a session cookie; the ticket is consumed.

    Needs no credential: the ticket is the credential. Redemption revokes the
    ticket row in one atomic statement, so a ticket opens at most one session,
    and only while it is unexpired and the credential that issued it is still
    current. An unknown, malformed, replayed, expired or orphaned ticket all
    get the same 401, and every failure counts against the login limiter
    (429 while locked), so tickets cannot be guessed at.
    """
    ip = request.client.host if request.client else "unknown"
    login_limiter: LoginRateLimiter = request.app.state.login_limiter
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

    mint = await _consume_login_ticket(request, body.ticket)
    if mint is None:
        login_limiter.record_failure(ip)
        return error_response(
            request,
            code="invalid_login_ticket",
            status=401,
            detail="invalid or expired login ticket",
            retryable=False,
        )

    login_limiter.record_success(ip)
    return await _open_session(request, mint, ip)


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
    token = request.cookies.get(cookie_name())
    fact = await asyncio.to_thread(
        current_session_fact,
        request.app.state.sessions,
        request.app.state.session_keys,
        token,
        settings.data_plane.cluster_secret,
        cache=request.app.state.machine_token_acceptance,
    )
    return JSONResponse(content={"authenticated": fact is not None})


def _sessions_that_authenticate(
    pool: Any, keys: SessionKeys, secret: str, *, cache: AcceptanceCache | None = None
) -> list[dict[str, Any]]:
    """Unrevoked, unexpired sessions whose mint is still admitted: the rows
    `current_session_fact` would accept. A rotated secret's sessions and ids without
    a mint are dead and omitted."""
    admitted = session_mints(keys, secret, cache=cache)
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
        cache=request.app.state.machine_token_acceptance,
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
