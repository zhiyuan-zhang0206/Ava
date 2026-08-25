"""Cluster-secret bearer auth + settings-free cookie helpers.

Two auth methods for the same cluster secret:
- Bearer token: `Authorization: Bearer <secret>` — SDK / agent / script.
- Session cookie: `ava_session=<opaque-id>` — browser (login flow); the gateway
  stores expiry and revocation state in Postgres.

The secret is a single per-cluster pre-shared key (`AVA_CLUSTER_SECRET`), set on
the gateway and handed to each agent-runner out-of-band for `ava enroll`. It
is presented as an HTTP `Authorization: Bearer <secret>` header and verified in
constant time. Pure stdlib (no shared.config import) so it is safe to use from
shared.bootstrap, which runs during the Settings import. Database-backed
session creation, validation, and revocation live in ``gateway.session_store``
so this foundational module stays importable without application settings.
"""

from __future__ import annotations

import hmac
from base64 import urlsafe_b64encode
from secrets import token_bytes

_SCHEME = "Bearer "
_COOKIE_NAME = "ava_session"
_DEFAULT_SESSION_TTL_SECONDS = 24 * 3600

# User-Agent the managed-browser daemon sends on its gateway login, so the
# sessions list can label its session rows and users can tell them apart from
# their own browser sessions (and avoid revoking the wrong one).
MANAGED_BROWSER_USER_AGENT = "ava-managed-browser"


def bearer_header(secret: str) -> dict[str, str]:
    """The Authorization header a caller presents to an authenticated surface."""
    return {"Authorization": f"{_SCHEME}{secret}"}


def verify_bearer(authorization: str | None, secret: str) -> bool:
    """True iff `authorization` carries exactly `Bearer <secret>`, compared in
    constant time. A blank configured `secret` never verifies — an unset
    cluster secret fails closed (rejects every caller) rather than open.
    """
    if not secret or not authorization or not authorization.startswith(_SCHEME):
        return False
    presented = authorization[len(_SCHEME) :]
    return hmac.compare_digest(presented, secret)


def new_session_id() -> str:
    """Return an opaque URL-safe identifier with 256 bits of entropy."""
    return urlsafe_b64encode(token_bytes(32)).rstrip(b"=").decode("ascii")


def cookie_name() -> str:
    """The session cookie name."""
    return _COOKIE_NAME


def session_cookie_header(
    token: str,
    *,
    secure: bool = False,
    ttl_seconds: int = _DEFAULT_SESSION_TTL_SECONDS,
) -> dict[str, str]:
    """Build a Set-Cookie header value for the session cookie.

    Args:
        token: an opaque server-side session identifier.
        secure: the gateway's effective cookie policy; if True, add ``Secure``.
        ttl_seconds: browser persistence lifetime in seconds.
    """
    # Without Max-Age Chromium treats this as a session cookie and drops it on
    # restart, forcing a re-login even while the server-side row is valid.
    flags = f"HttpOnly; SameSite=Lax; Path=/; Max-Age={ttl_seconds}"
    if secure:
        flags += "; Secure"
    return {"Set-Cookie": f"{_COOKIE_NAME}={token}; {flags}"}


def clear_cookie_header() -> dict[str, str]:
    """Build a Set-Cookie header that clears the session cookie."""
    return {
        "Set-Cookie": (
            f"{_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; "
            "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        )
    }


def is_managed_browser_user_agent(user_agent: str) -> bool:
    """True when ``user_agent`` identifies the managed-browser daemon's login."""
    return user_agent == MANAGED_BROWSER_USER_AGENT
