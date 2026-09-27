"""Server-bound credential identity and opt-in idempotency namespacing.

Caller labels are deliberately absent. A shared bearer authenticates exactly
one cluster administrator, not separate tools. Browser sessions minted from the
same cluster login authenticate that administrator too, even after token rotation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

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
    generation's token never matches: `shared.cluster.authority.api`). Both
    authenticate the one cluster administrator; the fact only records which
    credential did. A blank `secret` never verifies as the human bearer.
    """
    from shared.cluster.authority.api import acceptance, bearer_class
    from shared.cluster_auth import verify_bearer
    from shared.paths import ava_home

    if verify_bearer(authorization, secret):
        return "cluster_bearer"
    cls = bearer_class(authorization, acceptance(ava_home().resolve()))
    return None if cls is None else f"machine_token:{cls}"


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
