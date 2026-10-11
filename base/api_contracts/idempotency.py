"""Caller operation-key validation and stored identity shared by SDK and server boundaries."""

from __future__ import annotations

import hashlib
import json

SCOPE_HEADER = "Idempotency-Scope"
PRINCIPAL_SCOPE = "principal-v1"
_STORAGE_PREFIX = "principal-v1:"


def validate_idempotency_key(value: object) -> str:
    """Reject malformed supplied keys rather than minting another operation."""
    if not isinstance(value, str):
        raise TypeError("idempotency key must be a string")
    if not 1 <= len(value) <= 128:
        raise ValueError("idempotency key must contain 1 to 128 characters")
    return value


def keyed_headers(key: str) -> dict[str, str]:
    """The header pair every keyed request carries: the operation key and its scope."""
    return {"Idempotency-Key": validate_idempotency_key(key), SCOPE_HEADER: PRINCIPAL_SCOPE}


def stored_key(kind: str, subject: str, method: str, path: str, key: str) -> str:
    """Opaque stored identity of one operation: credential, route and caller key."""
    key = validate_idempotency_key(key)
    material = json.dumps([kind, subject, method.upper(), path, key])
    return _STORAGE_PREFIX + hashlib.sha256(material.encode()).hexdigest()


def chat_message_key(key: str) -> str:
    """Stored `client_message_id` of one chat message.

    A chat message id is cluster-wide: reusing it for another agent, body or
    source conflicts instead of delivering a second message. Only the cluster
    administrator reaches the chat routes, and the delivery outbox flush commits
    the same message without an HTTP request, so the identity is fixed to that
    principal rather than read from a request.
    """
    return stored_key("cluster", "administrator", "POST", "chat-message", key)
