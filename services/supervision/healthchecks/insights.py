"""Read-only identity probe for the insights service; the root supervisor owns recovery.

The service has no TCP port: it answers `/healthz` on its Unix socket. Alive means the
answer names this service, this home and the pid its pidfile records, so a stale socket
file or another home's process is not mistaken for it.
"""

from __future__ import annotations

from typing import cast

from base.daemon.health import DaemonProbe
from base.paths import ava_home, insights_pidfile, insights_socket

_TIMEOUT_S = 3.0


def _probe() -> DaemonProbe:
    import httpx

    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(insights_socket())),
            base_url="http://insights",
            timeout=_TIMEOUT_S,
        ) as client:
            resp = client.get("/healthz")
        resp.raise_for_status()
        payload = cast(object, resp.json())
    except Exception as exc:
        return DaemonProbe.down(
            f"GET /healthz on the insights socket failed ({type(exc).__name__}: {exc})"
        )
    if not isinstance(payload, dict):
        return DaemonProbe.down("the insights socket answered with a non-object")
    payload = cast(dict[str, object], payload)
    if payload.get("name") != "insights":
        return DaemonProbe.down("the insights socket answered as another service")
    if payload.get("home") != str(ava_home()):
        return DaemonProbe.down(f"the insights socket belongs to home {payload.get('home')!r}")
    try:
        recorded = int(insights_pidfile().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return DaemonProbe.down("no readable pid in the insights pidfile")
    if payload.get("pid") != recorded:
        return DaemonProbe.down(
            f"the answering pid {payload.get('pid')} is not the recorded {recorded}"
        )
    return DaemonProbe.up("insights answered /healthz as this home's recorded process")
