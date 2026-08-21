"""Milvus server healthcheck — called every 60s by the watchdog.

Probes with a real RPC — `MilvusClient.list_collections` against the cluster's
milvus URI (the same client path `services.memory_indexer` uses) — not a bare
TCP connect. A port-open probe certifies only that *some* process holds the
port, and it stays green while the server behind it is unusable; the memory
indexer's runtime calls then fail forever with nothing respawning milvus
(`conventions/defensive-patterns.md`: a health check must traverse what it
certifies — the 0004 lesson). `list_collections` breaks exactly when milvus
stops serving RPCs, so the watchdog restarts it.

Cost is bounded both ways: a healthy round is ~0.3s (client create +
`list_collections` on a loopback server), a dead server fails the client
connect within `_TIMEOUT_S`. On death, `shared.service_respawn.respawn_service`
starts a new daemon in the ava-milvus pane (same pattern as the other daemon
healthchecks; daemon enters the session and does not detach).
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

from shared.config import settings
from shared.log import init_gateway_process
from shared.service_respawn import respawn_service

_log = logging.getLogger("services.healthchecks.milvus")

_TIMEOUT_S = 3.0


def _is_alive() -> bool:
    """True when milvus answers a real RPC; any failure means "not alive".

    The probe fails closed: an unforeseen exception (a wedged server, a foreign
    process on the port that does not speak milvus) degrades to "dead" — a
    verdict the watchdog can act on — instead of to no answer at all. The import
    stays inside the function so the watchdog's own import of this module stays
    light.
    """
    from pymilvus import MilvusClient

    client: MilvusClient | None = None
    try:
        client = MilvusClient(uri=settings.services.milvus_uri, timeout=_TIMEOUT_S)
        # The real MilvusClient is sync; the stubs type it as an async Unknown.
        client.list_collections()  # pyright: ignore[reportUnknownMemberType, reportUnusedCoroutine]
        return True
    except Exception as exc:
        _log.debug(
            "[milvus healthcheck] probe failed (%s: %s); treating as dead",
            type(exc).__name__,
            exc,
        )
        return False
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _restart_daemon() -> bool:
    """Start milvus in the ava-milvus pane via ``shared.service_respawn.respawn_service``."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_service(
        "milvus",
        ".venv/bin/python -m services.milvus.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
    )


def main() -> None:
    init_gateway_process(name="milvus-healthcheck")

    if _is_alive():
        _log.debug("[milvus healthcheck] server alive, no-op")
        return

    _log.info("[milvus healthcheck] server dead, restarting...")
    if _restart_daemon():
        _log.info("[milvus healthcheck] daemon restarted successfully")
    else:
        _log.error("[milvus healthcheck] daemon restart FAILED — manual intervention needed")
        sys.exit(1)


if __name__ == "__main__":
    main()
