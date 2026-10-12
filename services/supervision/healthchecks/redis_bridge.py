"""Read-only health probe for the Redis private-network bridge; the root supervisor owns recovery."""

from __future__ import annotations

import socket

from base.daemon.health import DaemonProbe

_TIMEOUT_S = 3.0


def probe_relay(host: str, port: int) -> DaemonProbe:
    """Alive when a RESP command sent through the relay gets a RESP reply from Redis.

    The relay never writes to a client itself, so any reply (`+PONG`, or `-NOAUTH` from an
    authenticated Redis) proves the listener forwards to the backend.
    """
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT_S) as connection:
            connection.settimeout(_TIMEOUT_S)
            connection.sendall(b"PING\r\n")
            reply = connection.recv(64)
    except OSError as exc:
        return DaemonProbe.down(f"relay {host}:{port} did not answer ({type(exc).__name__}: {exc})")
    if reply[:1] in (b"+", b"-"):
        return DaemonProbe.up(f"relay {host}:{port} forwarded a Redis reply")
    return DaemonProbe.down(f"relay {host}:{port} returned a non-RESP reply")
