"""Session auth endpoints — login, logout, check.

POST /api/auth/login   — verify password, set session cookie
POST /api/auth/logout  — clear session cookie
GET  /api/auth/check   — report whether the current session is valid
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway.error_envelope import error_response
from shared.cluster_auth import (
    clear_cookie_header,
    cookie_name,
    session_cookie_header,
    sign_session,
    verify_session,
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
    cookie valid for 7 days.

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

    # Constant-time comparison to avoid timing attacks
    import hmac

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
    token = sign_session(secret)
    secure = request.url.scheme == "https"
    headers = session_cookie_header(token, secure=secure)
    return JSONResponse(content={"ok": True}, headers=headers)


@router.post("/api/auth/logout")
async def logout() -> JSONResponse:
    """Clear the session cookie — no auth required (idempotent)."""
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
    secret = settings.data_plane.cluster_secret
    authenticated = verify_session(token, secret)
    return JSONResponse(content={"authenticated": authenticated})
