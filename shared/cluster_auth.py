"""Cluster-secret bearer auth + cookie session auth.

Two auth methods for the same cluster secret:
- Bearer token: `Authorization: Bearer <secret>` — SDK / agent / script.
- Session cookie: `ava_session=<signed-token>` — browser (login flow).

The secret is a single per-cluster pre-shared key (`AVA_CLUSTER_SECRET`), set on
the gateway and handed to each agent-runner out-of-band at `ava host enroll`. It
is presented as an HTTP `Authorization: Bearer <secret>` header and verified in
constant time. Pure stdlib (no shared.config import) so it is safe to use from
shared.bootstrap, which runs during the Settings import.

Revocation note (audit round-2 security P3): session cookies are
self-contained HMAC tokens with no server-side store — there is no
per-session revocation. A leaked cookie stays valid until its 7-day expiry
or until the cluster secret is rotated (which invalidates every session and
every bearer at once); `scripts/rotate_cluster_secret.py` is the rotation
path. Treat a session cookie as a cluster-secret-adjacent credential.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

_SCHEME = "Bearer "
_COOKIE_NAME = "ava_session"
# Session lifetime: 7 days. Long enough to not interrupt daily work,
# short enough to not be a permanent credential.
_SESSION_TTL = 7 * 24 * 3600


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


def sign_session(secret: str, ttl: int = _SESSION_TTL) -> str:
    """Create a signed, time-limited session token.

    The token is a self-contained base64-encoded blob: ``expiry.mac`` where
    ``mac = HMAC-SHA256(expiry, secret)``. No server-side storage needed.
    """
    expiry = int(time.time()) + ttl
    expiry_bytes = str(expiry).encode()
    mac = hmac.new(secret.encode(), expiry_bytes, hashlib.sha256).digest()
    token_bytes = expiry_bytes + b"." + mac
    return urlsafe_b64encode(token_bytes).rstrip(b"=").decode()


def verify_session(token: str | None, secret: str) -> bool:
    """Verify a session token. Returns False if missing, expired, or invalid.

    Compared in constant time against the expected MAC so an attacker
    cannot use timing to forge a valid token.
    """
    if not token or not secret:
        return False
    try:
        # urlsafe_b64decode needs padding
        padded = token + "=" * (4 - len(token) % 4)
        data = urlsafe_b64decode(padded)
        expiry_bytes, mac = data.split(b".", 1)
        expiry = int(expiry_bytes)
        if expiry < time.time():
            return False
        expected_mac = hmac.new(secret.encode(), expiry_bytes, hashlib.sha256).digest()
        return hmac.compare_digest(mac, expected_mac)
    except Exception:
        return False


def cookie_name() -> str:
    """The session cookie name."""
    return _COOKIE_NAME


def session_cookie_header(token: str, *, secure: bool = False) -> dict[str, str]:
    """Build a Set-Cookie header value for the session cookie.

    Args:
        token: a signed token from ``sign_session``.
        secure: if True, add ``Secure`` flag (requires HTTPS).
    """
    # Max-Age mirrors the token's own TTL: without it Chromium treats the
    # cookie as a session cookie and never persists it, so every app/browser
    # restart forces a re-login (desktop #706).
    flags = f"HttpOnly; SameSite=Lax; Path=/; Max-Age={_SESSION_TTL}"
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
