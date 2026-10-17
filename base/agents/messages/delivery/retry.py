"""Explicit gateway failures eligible for bounded retry and deferred delivery."""

import json
from typing import Any, Protocol, cast

# 500 is an unknown server failure. The caller must see its original response.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})


class GatewayResponse(Protocol):
    """The slice of ``httpx.Response`` the retry policy reads.

    Declared structurally so this module, which loads on the ``import meiva`` path through the
    gateway transport, does not import httpx at module level.
    """

    @property
    def status_code(self) -> int: ...

    def json(self, **kwargs: Any) -> Any: ...


def network_errors() -> tuple[type[Exception], ...]:
    """The httpx transport failures that leave the gateway reachable on a later attempt."""
    import httpx  # deferred: `import meiva` reaches this module and must not load the HTTP stack

    return (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    )


def retryable_response(response: GatewayResponse) -> bool:
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
