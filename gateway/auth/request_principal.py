"""Server-bound credential identity and opt-in idempotency namespacing.

Caller labels are deliberately absent. A shared bearer authenticates exactly
one cluster administrator, not separate tools. Browser sessions minted from the
same cluster login authenticate that administrator too, but only while the
credential that minted them is still current (`session_mints`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from starlette.requests import Request

SCOPE_HEADER = "Idempotency-Scope"
PRINCIPAL_SCOPE = "principal-v1"
_STORAGE_PREFIX = "principal-v1:"


@dataclass(frozen=True)
class AuthPrincipal:
    """Construct only after credential verification, never from request JSON."""

    kind: Literal["cluster", "mcp_client"]
    subject: str


class PrincipalScopeError(ValueError):
    """The requested key scope cannot be honored before any durable write."""


def cluster_credential(authorization: str | None, secret: str) -> str | None:
    """The credential fact of a cluster bearer, or None when it presents none.

    `cluster_bearer` for the human cluster secret; `machine_token:<class>` for
    the ACTIVE write generation's machine API token of that class (a revoked
    generation's token never matches: `base.cluster.authority.api`). Both
    authenticate the one cluster administrator; the fact only records which
    credential did. A blank `secret` never verifies as the human bearer.
    """
    from base.cluster.auth import verify_bearer
    from base.cluster.authority.api import acceptance, bearer_class
    from base.paths import ava_home

    if verify_bearer(authorization, secret):
        return "cluster_bearer"
    cls = bearer_class(authorization, acceptance(ava_home().resolve()))
    return None if cls is None else f"machine_token:{cls}"


# ── browser sessions bound to their minting credential ─────────────────────
#
# A session id is `<mint>.<random>`. The mint names the credential that logged
# the browser in: `human-<mac>` (the human cluster secret) or `runner-<mac>` (a
# write generation's runner API token, which a unit's managed browser
# presents). `<mac>` is HMAC-SHA256 under this gateway's private session key
# over the credential's SHA-256 digest, so a cookie carries no form of the
# credential that can be brute-forced offline. A session authenticates only
# while its mint is one of the CURRENT credentials' mints: the fence revoking a
# generation, or a rotated human secret, ends every session that credential
# minted at once, with no revocation step at any rotation point.

SESSION_KEY_NAME = "web-session.key"
_SESSION_KEY = re.compile(r"^[0-9a-f]{64}$")
_MINT_FACTS = {"human": "user_session", "runner": "machine_session:runner"}
_session_key_cache: dict[Path, tuple[tuple[int, int, int], bytes]] = {}


def session_key_path(home: Path) -> Path:
    return home / SESSION_KEY_NAME


def _session_key(home: Path, *, create: bool) -> bytes | None:
    """This gateway's session-mint MAC key: minted once, at the first login.

    None when absent and `create` is false (then no session authenticates).
    Losing the file ends every session; the next login mints a new key.
    """
    from base.deploy.release.verified_file import regular_bytes
    from base.host.private_storage import create_private_bytes, private_file_problem

    path = session_key_path(home)
    if create and not path.exists():
        with suppress(FileExistsError):
            create_private_bytes(path, (secrets.token_hex(32) + "\n").encode())
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    identity = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _session_key_cache.get(home)
    if cached is not None and cached[0] == identity:
        return cached[1]
    problem = private_file_problem(path)
    if problem is None and os.name != "nt" and info.st_mode & 0o077:
        problem = "is not owner-only"
    if problem is not None:
        raise RuntimeError(f"{path}: {problem}")
    value = regular_bytes(path, max_bytes=4096).decode().strip()
    if _SESSION_KEY.fullmatch(value) is None:
        raise RuntimeError(f"{path} does not hold a session key")
    key = bytes.fromhex(value)
    _session_key_cache[home] = (identity, key)
    return key


def _mint(key: bytes, kind: str, credential_digest: str) -> str:
    mac = hmac.new(key, f"{kind}\0{credential_digest}".encode(), hashlib.sha256)
    return f"{kind}-{mac.hexdigest()[:32]}"


def login_mint(password: str, secret: str) -> str | None:
    """The mint of the credential a login `password` presents, else None.

    The human secret, or the ACTIVE generation's runner API token (a unit's
    managed browser holds no human secret). The gateway-class token never logs
    a browser in. Mints this gateway's session key on first use.
    """
    from base.cluster.authority.api import acceptance, bearer_class, token_digest
    from base.paths import ava_home

    home = ava_home().resolve()
    if secret and hmac.compare_digest(password, secret):
        kind, digest = "human", token_digest(secret)
    else:
        accepted = acceptance(home)
        if bearer_class(f"Bearer {password}", accepted) != "runner":
            return None
        kind, digest = "runner", accepted["runner"]
    key = _session_key(home, create=True)
    if key is None:
        raise RuntimeError(f"{session_key_path(home)} vanished while minting a session")
    return _mint(key, kind, digest)


def session_mints(secret: str) -> dict[str, str]:
    """{mint: credential fact} of the credentials that may back a session now.

    The fact is `user_session` for the human secret and `machine_session:runner`
    for the active generation's runner token. Empty while no session key
    exists; the runner mint is absent while no generation is active (a fence).
    """
    from base.cluster.authority.api import acceptance, token_digest
    from base.paths import ava_home

    home = ava_home().resolve()
    key = _session_key(home, create=False)
    if key is None:
        return {}
    mints: dict[str, str] = {}
    if secret:
        mints[_mint(key, "human", token_digest(secret))] = _MINT_FACTS["human"]
    runner = acceptance(home).get("runner")
    if runner is not None:
        mints[_mint(key, "runner", runner)] = _MINT_FACTS["runner"]
    return mints


# The facts of the human cluster secret: its bearer and the sessions it minted.
# Every other fact (`machine_token:<class>`, `machine_session:runner`) is bound
# to a write generation and dies with it.
HUMAN_CREDENTIAL_FACTS = frozenset({"cluster_bearer", _MINT_FACTS["human"]})


def require_human_credential(request: Request) -> None:
    """Route dependency: 403 unless the human secret authenticated `request`.

    Guards routes that mint a credential outliving any write generation (MCP
    client tokens): a generation-bound admission must not leave behind a
    credential its own revocation cannot end. An open API (no secret, or the
    middleware switched off) has no credential to tell apart and passes.
    """
    from fastapi import HTTPException

    from base.config import settings

    if not settings.gateway.auth_middleware_enabled or not settings.data_plane.cluster_secret:
        return
    if getattr(request.state, "source_verified_by", None) not in HUMAN_CREDENTIAL_FACTS:
        raise HTTPException(
            status_code=403,
            detail="requires the human cluster secret or a session it minted; "
            "a write generation's credential cannot manage credentials that outlive it",
        )


def current_session_fact(pool: Any, session_id: str | None, secret: str) -> str | None:
    """The credential fact of a valid session whose minting credential is
    current (`user_session` / `machine_session:runner`), else None: the one
    session check the auth middleware and `/api/auth/check` share."""
    from gateway.auth.session_store import session_is_valid, session_mint

    mint = None if session_id is None else session_mint(session_id)
    if mint is None:
        return None
    mints = session_mints(secret)
    if not session_is_valid(pool, session_id, admitted=mints):
        return None
    return mints[mint]


def principal_key(principal: AuthPrincipal, method: str, path: str, key: str) -> str:
    """Stable opaque key under actual credential + logical operation identity."""
    if not key or len(key) > 128:
        raise PrincipalScopeError("idempotency key must contain 1 to 128 characters")
    material = json.dumps([principal.kind, principal.subject, method.upper(), path, key])
    return _STORAGE_PREFIX + hashlib.sha256(material.encode()).hexdigest()


def request_key(request: Request, key: str, *, method: str, path: str) -> str:
    """Preserve legacy retries unless the caller explicitly chooses v1.

    The reserved storage prefix cannot be submitted as a raw legacy key: doing
    so would allow a legacy request to address another principal's stored reply.
    """
    scope = request.headers.get(SCOPE_HEADER)
    principal = getattr(request.state, "auth_principal", None)
    if isinstance(principal, AuthPrincipal) and principal.kind == "mcp_client":
        if scope not in (None, PRINCIPAL_SCOPE):
            raise PrincipalScopeError("MCP token keys require principal-v1 scope")
        return principal_key(principal, method, path, key)
    if scope is None:
        if key.startswith(_STORAGE_PREFIX):
            raise PrincipalScopeError("reserved idempotency storage prefix; choose a client key")
        return key
    if scope != PRINCIPAL_SCOPE:
        raise PrincipalScopeError("unsupported Idempotency-Scope; expected principal-v1")
    if not isinstance(principal, AuthPrincipal):
        raise PrincipalScopeError("principal-v1 requires a verified credential principal")
    return principal_key(principal, method, path, key)
