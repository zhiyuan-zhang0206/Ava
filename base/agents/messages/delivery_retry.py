"""Explicit gateway failures eligible for bounded retry and deferred delivery."""

import json
from typing import cast

import httpx

# 500 is an unknown server failure. The caller must see its original response.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})
NETWORK_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


def retryable_response(response: httpx.Response) -> bool:
    """Known transient status, unless the wire refuses retry or proves commit.

    A proxy's non-JSON error page retains its status policy. Structured gateway
    responses may narrow it; malformed boolean controls are protocol errors.
    The response itself stays intact so callers retain the durable receipt.
    """
    if response.status_code not in TRANSIENT_HTTP_STATUSES:
        return False
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(body, dict):
        return True
    wire = cast(dict[str, object], body)
    for field in ("retryable", "committed"):
        if field in wire and not isinstance(wire[field], bool):
            raise ValueError(f"Gateway response {field} must be a boolean")
    return wire.get("retryable") is not False and wire.get("committed") is not True


def retryable_database_error(error: Exception) -> bool:
    """Database connection availability, excluding pool lifecycle and query errors."""
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    return (
        isinstance(error, PoolTimeout)
        or (type(error) is OperationalError and error.sqlstate is None)
        or (
            isinstance(error, OperationalError)
            and error.sqlstate is not None
            and error.sqlstate.startswith("08")
        )
    )
