"""Memory search service healthcheck — called every 60s by the watchdog.

Probes with a real search (POST /search with a zero vector) against the
service's HTTP port — not a bare TCP connect and not /healthz. A
port-open probe certifies only that *some* process holds the port; the
/healthz handler certifies only that the event loop turns. A real search
traverses the store and the search path, so it breaks exactly when the
service stops serving (`conventions/defensive-patterns.md`: a health
check must traverse what it certifies).

Lifecycle follows the shared keepalive policy
(`shared.service_respawn.run_keepalive` — probe / respawn-and-verify /
exponential backoff / circuit breaker / PORT_TAKEN terminal verdict;
decision `decisions/2026-08-29-watchdog-respawn-backoff-breaker.md`), the
same body as the memory-indexer healthcheck. The respawn is reported by
the probe, not by the spawn: `respawn_and_verify` returns the daemon's
own verdict, so a daemon that crashes on a bound port or a broken cache
is never logged as "restarted successfully", and a condition a respawn
cannot cure backs off and trips the breaker instead of restarting every
60s forever.
"""

from __future__ import annotations

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.memory_search")

_TIMEOUT_S = 3.0


def _post_search(uri: str) -> dict[str, object] | DaemonProbe:
    """One POST /search with a zero vector — the body dict when it
    answered, or the not-alive verdict that trying produced (fail
    closed: any failure means "not alive", per the module docstring)."""
    import httpx

    from services.memory_indexer.embedder import DIM

    try:
        resp = httpx.post(
            f"{uri}/search",
            json={"vector": [0.0] * DIM, "k": 1},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return DaemonProbe.down(f"POST /search failed ({type(exc).__name__}: {exc})")


def _probe() -> DaemonProbe:
    """Alive when the service answers a real search with a paths payload."""
    payload = _post_search(settings.services.memory_search_uri)
    if isinstance(payload, DaemonProbe):
        return payload
    if "paths" in payload:
        return DaemonProbe.up("POST /search answered with a paths payload")
    return DaemonProbe.down("POST /search answered without a paths payload")


def _restart_daemon() -> DaemonProbe:
    """Start the daemon in the memory-search pane, then confirm it actually
    came up — `respawn_and_verify` polls the probe until it proves alive,
    because a successful spawn is not evidence the daemon serves."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "memory-search",
        ".venv/bin/python -m services.memory_search.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="memory_search-healthcheck")
    run_keepalive("memory_search", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
