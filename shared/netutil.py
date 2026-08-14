"""Config-free networking predicates.

Kept dependency-free (no `shared.config`, which instantiates Settings at import
and fails on a fresh un-enrolled host) so `cli/enroll.py` — which runs before a
host has a full config — can import it alongside `shared.machine`.
"""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """True when `host` names a loopback address — `localhost`, the 127.0.0.0/8
    block, `0.0.0.0`, or IPv6 `::1` (bracketed or bare).

    A loopback address is only reachable from the same box, so a *remote* node
    that advertises one is misconfigured: the dialer would hit itself instead of
    the peer. `cli/enroll.py` and the machine-registration path
    (`shared.machines.register_self`) use this to refuse a loopback reachable
    address for a host the gateway must reach over the network.
    """
    h = host.strip().lower().removeprefix("[").removesuffix("]")
    return h in ("localhost", "::1", "0.0.0.0") or h.startswith("127.")  # noqa: S104 — 0.0.0.0 classified as non-reachable, not a bind address


def is_ipv4_literal(host: str) -> bool:
    """True when `host` is already a dotted-quad IPv4 address — no name to
    resolve, so a dialer can (and, on a DNS64/NAT64 network, must) connect to
    it directly instead of calling `getaddrinfo`.

    Some IPv6-only networks (observed on a cellular hotspot running 464XLAT
    CLAT) synthesize an AAAA answer even for a `getaddrinfo` call on a plain
    IPv4 literal, routing the connection through the carrier's own NAT64
    gateway; when the literal is CGNAT/private space (this cluster's
    tailscale 100.64.0.0/10 addresses), that gateway has no route back and
    the dial hangs or times out. `shared/http_dial.py` (httpx) and
    `shared/config/data_plane.py` (`hostaddr=`, for Postgres) use this
    predicate to bypass resolution for exactly the hosts where it can never
    be anything other than itself.

    A hostname (including one that *resolves* to a single A record) is not a
    literal — this returns False for it, and its dial goes through the
    ordinary resolver unaffected. An IPv6 literal also returns False: this
    predicate is specifically about the IPv4-literal-gets-v6-synthesized
    failure mode, not a general "is this pre-resolved" check.
    """
    h = host.strip().removeprefix("[").removesuffix("]")
    try:
        return isinstance(ipaddress.ip_address(h), ipaddress.IPv4Address)
    except ValueError:
        return False
