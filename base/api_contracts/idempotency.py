"""Caller operation-key validation shared by SDK and server boundaries."""

from __future__ import annotations


def validate_idempotency_key(value: object) -> str:
    """Reject malformed supplied keys rather than minting another operation."""
    if not isinstance(value, str):
        raise TypeError("idempotency key must be a string")
    if not 1 <= len(value) <= 128:
        raise ValueError("idempotency key must contain 1 to 128 characters")
    return value
