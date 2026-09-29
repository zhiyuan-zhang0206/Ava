"""Read-only health probes for milvus; the root supervisor owns recovery."""

from __future__ import annotations

import contextlib
import logging

from shared.config import settings

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
