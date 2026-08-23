"""IM Bridge healthcheck — run by the gateway watchdog every 60s.

Checks whether the im_bridge daemon is alive:
- alive -> no-op
- this unit's daemon holds the port but reports stale -> warn + no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn

Usage (watchdog):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.im_bridge
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

import shared.daemon_health
import shared.paths
from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

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


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    # The respawn session name MUST match ServiceSpec.session ("im-bridge",
    # kebab-case) — respawn_service composes the session name from it, and
    # `ava status` / `ava stop` / `ava restart` look up the same name. The
    # module name ("im_bridge") differs from the session name for this service,
    # and a respawn under the module name strands the session where the CLI
    # cannot see or kill it (Task #1291).
    return respawn_and_verify(
        "im-bridge",
        ".venv/bin/python -m services.im_bridge.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="im_bridge-healthcheck")
    run_keepalive("im_bridge", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
