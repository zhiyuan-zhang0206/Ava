"""Weak-network resilience values shared by the sync + async redis clients.

Single source for the keepalive / connect-timeout tuning that
``shared.redis_client`` and ``shared.redis_listener`` both apply (audit
2026-08-08 P2: the listener used to replicate these by hand under a comment
admitting "the values are kept in sync" — the exact manual-sync drift
surface R2 exists to kill). Deliberately dependency-free (stdlib only) so
either client can import it without a cycle.

Why these values: the central Postgres/Redis sit behind a TLS-MITM corp
link; a half-dead connection must surface in tens of seconds, not the OS
default TCP timeout (minutes) that once hung exec output streaming on a
Redis hiccup. Keepalive: ~30s idle, then probe every 10s, 3 misses = dead.
`getattr` keeps the option names portable — the Linux-only TCP_KEEPIDLE
constant is absent from the macOS socket module (TCP_KEEPALIVE is the macOS
spelling).
"""

from __future__ import annotations

import socket

_SOCKET_CONNECT_TIMEOUT_S = 5.0
_HEALTH_CHECK_INTERVAL_S = 30


def keepalive_options() -> dict[int, int]:
    """TCP keepalive probe tuning, emitting only the options the running
    kernel defines (the option names differ across Linux / macOS)."""
    opts: dict[int, int] = {}
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    if idle is not None:
        opts[idle] = 30
    intvl = getattr(socket, "TCP_KEEPINTVL", None)  # seconds between probes
    if intvl is not None:
        opts[intvl] = 10
    cnt = getattr(socket, "TCP_KEEPCNT", None)  # failed probes before giving up
    if cnt is not None:
        opts[cnt] = 3
    return opts
