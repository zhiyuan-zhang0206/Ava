"""Milvus server healthcheck — called every 60s by the watchdog.

TCP probe `127.0.0.1:<port>` (gRPC listening = ready). On death,
`shared.service_respawn.respawn_service` starts a new daemon in the
ava-milvus pane (same pattern as the 5 daemon healthchecks in #257;
daemon enters the session and does not detach).

Does not probe via `MilvusClient.list_collections` — pymilvus client
creation + gRPC handshake itself is slow, and the watchdog 60s cycle
does not want to wait. TCP listening = milvus-lite server process is
alive, which is enough.
"""

import logging
import socket
import sys
from pathlib import Path

from shared.config import settings
from shared.log import init_gateway_process
from shared.service_respawn import respawn_service

_log = logging.getLogger("services.healthchecks.milvus")

_PORT = settings.services.milvus_port
_TIMEOUT_S = 3.0


def _is_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", _PORT), timeout=_TIMEOUT_S):
            return True
    except OSError:
        return False


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
