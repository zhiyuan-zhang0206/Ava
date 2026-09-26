"""Read-only health probes for im bridge; the root supervisor owns recovery."""

import json
import logging
import urllib.error
import urllib.request
from typing import cast

import shared.daemon_health
import shared.paths
from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port

_log = logging.getLogger("services.healthchecks.im_bridge")

_HEALTH_URL = (
    settings.services.im_bridge_health_url or f"http://localhost:{health_port('im_bridge')}/healthz"
)


def _holder_payload() -> dict[str, object] | None:
    """Read the bound port's identity, including a liveness-stale 503 body."""
    try:
        try:
            with urllib.request.urlopen(  # noqa: S310 — loopback URL from settings
                _HEALTH_URL, timeout=shared.daemon_health._PROBE_TIMEOUT_S
            ) as response:
                body = response.read(shared.daemon_health._MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            body = exc.read(shared.daemon_health._MAX_BODY_BYTES)
        parsed: object = json.loads(body)
    except Exception:
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


def _probe() -> DaemonProbe:
    """Use the shared verdict unless our own daemon still holds the port.

    The bridge can legitimately block its work loop on an IM long poll, so its
    liveness-stale 503 is not sufficient evidence that respawning is safe.
    """
    result = shared.daemon_health.probe_daemon(
        "im_bridge", _HEALTH_URL, pidfile=settings.services.im_bridge_pidfile
    )
    if result.alive or result.terminal:
        return result

    payload = _holder_payload()
    if (
        payload is None
        or payload.get("name") != "im_bridge"
        or payload.get("home") != str(shared.paths.ava_home())
    ):
        return result

    holder_pid = payload.get("pid")
    stale_for = payload.get("stale_for")
    _log.warning(
        "[im_bridge healthcheck] suppressing respawn: our daemon holds the health port "
        "(holder pid=%r, stale_for=%r)",
        holder_pid,
        stale_for,
    )
    return DaemonProbe.up(f"own port holder pid={holder_pid!r}, stale_for={stale_for!r}")
