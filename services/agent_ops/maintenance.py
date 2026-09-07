"""Keep admitted ops and uncancelled worker futures visible during local stop."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from shared import maintenance

_requests: set[object] = set()
_workers: set[asyncio.Future[Any]] = set()


@contextmanager
def admission(kind: str) -> Generator[None]:
    """Keep readiness and generation-checked resume reachable during a hold.

    Both remain counted until their actual work finishes. Resume owns its
    readiness, failure-receipt and exact-generation checks in ops_cluster;
    accepting the request is not permission to release native admission.
    """
    current = maintenance.snapshot()
    if (
        current is not None
        and kind not in ("status_probe", "cluster_resume")
        and current.maintenance is not None
        and current.maintenance.phase in ("stopping", "stopped", "starting", "ready")
    ):
        raise RuntimeError("unit is stopping or held for explicit maintenance resume")
    token = object()
    _requests.add(token)
    try:
        yield
    finally:
        _requests.remove(token)


def track_worker(future: asyncio.Future[Any]) -> None:
    _workers.add(future)
    future.add_done_callback(_workers.discard)


def progress() -> dict[str, int]:
    return {"protocol": 1, "requests": len(_requests), "workers": len(_workers)}
